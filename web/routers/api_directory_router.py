"""GET /api/api-directory — "API 发布" board for the 实时总览 page.

Project_Task.md「实时总览」章节: list the important Web/API endpoints —
name, purpose, base URL, and an auth hint — plus the SearXNG search URL
format. This is a read-only directory built from live state
(web.state.registry for currently-loaded models, core.config for ports),
not a static hardcoded list, so it always reflects what's actually
reachable right now.

IP display: web_host/0.0.0.0 is a bind address, not something a client on
another machine can connect to. Showing "127.0.0.1" there is actively wrong
for this project's actual usage — every consumer (the Vue frontend, curl
from a teammate's laptop, an external client app) reaches this server over
the LAN, never from localhost on the server itself. So the displayed host
is detected as the server's real outbound LAN IP (via a UDP "connect" that
never actually sends a packet — the standard no-DNS, no-internet-required
trick for asking the OS routing table which local interface would be used).

Secret handling: the admin password/secret_key are deliberately NOT
repeated here even though settings_router.py already exposes them in
plaintext — duplicating the same secret across two endpoints just creates
two places that need to stay in sync. This endpoint only points the user at
高级设置 (Settings page / GET /api/settings) for that value. The vllm
--api-key IS shown here in full (sk-xxxx shape), since it's specifically
the credential OpenAI/Claude-compatible clients need to fill in to call
these endpoints — that's the whole point of this board.
"""
from __future__ import annotations

import socket
import time

import httpx
from fastapi import APIRouter, Depends

from core import searxng_client
from core.config import load_config
from web.auth import get_current_user
from web.state import registry

router = APIRouter(prefix="/api/api-directory", tags=["api-directory"], dependencies=[Depends(get_current_user)])

# Per-probe timeout for the active health check. Kept short so a single
# unreachable endpoint can't make the whole "检测健康状态" button hang —
# a loaded-but-still-warming-up vllm container answers /v1/models within
# well under a second once its HTTP server is up.
_HEALTH_PROBE_TIMEOUT = 5.0


def _detect_lan_ip() -> str | None:
    """Best-effort detection of the host's outbound LAN IP.

    Opens a UDP socket and "connects" it to a public IP without ever
    sending a packet — the OS still has to pick a local source address for
    the route, which is exactly the LAN-facing IP other machines use to
    reach this server. Falls back to None (caller decides the fallback
    display value) if the host has no route at all (e.g. fully offline).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _host_for_display(web_host: str) -> str:
    if web_host not in ("0.0.0.0", "::"):
        # An explicit non-wildcard bind address was configured — trust it.
        return web_host
    return _detect_lan_ip() or "127.0.0.1"


def _endpoint_specs(config, host: str) -> list[dict]:
    """Single source of truth for the published endpoints.

    Both GET /api/api-directory (the 发布 board) and GET .../health-check
    derive from this list, so the displayed rows and their health results
    can never drift apart by name (a real bug we hit before, when the two
    were built independently). Each spec carries display fields
    (name/purpose/base_url/auth_hint) plus a `probe` describing how
    health-check should test it.

    The 对外网关 (gateway_router on the Web backend port) is listed FIRST as
    the recommended single, stable external entry — OpenAI & Claude clients
    point here regardless of which container/port a model currently uses.
    Direct-to-container rows are kept (marked 调试) for troubleshooting.
    """
    api_key = config.vllm_api_key
    web = config.web_port
    bearer = {"Authorization": f"Bearer {api_key}"} if api_key else None
    gw_probe = {"kind": "http", "url": f"http://127.0.0.1:{web}/v1/models", "headers": bearer}

    loaded_names = registry.all_loaded_names()
    general_model_name = next((n for n in loaded_names if not _is_embedding_loaded(n)), None)
    embedding_model_name = next((n for n in loaded_names if _is_embedding_loaded(n)), None)

    specs: list[dict] = []

    specs.append({
        "name": "对外网关 - OpenAI接口",
        "purpose": f"OpenAI 兼容统一入口（/v1/chat/completions、/v1/completions、/v1/models），"
                   f"按请求体 model 字段自动路由到已加载模型（当前通用模型：{general_model_name or '无'}）",
        "base_url": f"http://{host}:{web}/v1",
        "auth_hint": f"Authorization: Bearer {api_key}",
        "probe": gw_probe if general_model_name else {"kind": "unavailable", "detail": "当前无加载的通用模型，对话类请求会返回404"},
    })
    specs.append({
        "name": "对外网关 - Claude接口",
        "purpose": f"Claude(Anthropic) Messages 统一入口（/v1/messages、/v1/messages/count_tokens），"
                   f"当前通用模型：{general_model_name or '无'}",
        "base_url": f"http://{host}:{web}/v1/messages",
        # The gateway accepts BOTH Bearer and x-api-key (unlike raw vllm which
        # only takes Bearer) — so a standard Claude client works against it.
        "auth_hint": f"Authorization: Bearer {api_key}  （或 x-api-key: {api_key}）",
        "probe": gw_probe if general_model_name else {"kind": "unavailable", "detail": "当前无加载的通用模型"},
    })
    specs.append({
        "name": "对外网关 - Embedding接口",
        "purpose": f"OpenAI 兼容向量化统一入口（/v1/embeddings），当前嵌入模型：{embedding_model_name or '无'}",
        "base_url": f"http://{host}:{web}/v1/embeddings",
        "auth_hint": f"Authorization: Bearer {api_key}",
        "probe": gw_probe if embedding_model_name else {"kind": "unavailable", "detail": "当前无加载的嵌入模型"},
    })

    specs.append({
        "name": "Web管理后台API",
        "purpose": "FastAPI 后端：登录鉴权、模型管理、组件控制、环境检测、设置等全部管理接口",
        "base_url": f"http://{host}:{web}/api",
        "auth_hint": "Bearer JWT（POST /api/auth/login 登录获取，置于 Authorization: Bearer <token> 请求头）",
        "probe": {"kind": "self"},
    })

    gen_loaded = registry.get(general_model_name) if general_model_name else None
    if gen_loaded and gen_loaded.host_port:
        specs.append({
            "name": "直连-通用模型容器(调试)",
            "purpose": f"直连通用模型容器「{general_model_name}」（绕过网关排障用），原生 vllm 仅认 Bearer",
            "base_url": f"http://{host}:{gen_loaded.host_port}/v1",
            "auth_hint": f"Authorization: Bearer {api_key}",
            "probe": {"kind": "http", "url": f"http://{host}:{gen_loaded.host_port}/v1/models", "headers": bearer},
        })
    emb_loaded = registry.get(embedding_model_name) if embedding_model_name else None
    if emb_loaded and emb_loaded.host_port:
        specs.append({
            "name": "直连-嵌入模型容器(调试)",
            "purpose": f"直连嵌入模型容器「{embedding_model_name}」（绕过网关排障用）",
            "base_url": f"http://{host}:{emb_loaded.host_port}/v1/embeddings",
            "auth_hint": f"Authorization: Bearer {api_key}",
            "probe": {"kind": "http", "url": f"http://{host}:{emb_loaded.host_port}/v1/models", "headers": bearer},
        })

    specs.append({
        "name": "SearXNG本地搜索",
        "purpose": "本地部署的 SearXNG 搜索引擎 JSON 检索接口，供调试/集成检索结果使用",
        "base_url": (
            f"http://{host}:{config.searxng_port}/search?q=<query>&format=json"
            "&categories=general&language=auto&safesearch=0"
        ),
        "auth_hint": "无需鉴权",
        "probe": {"kind": "searxng"},
    })

    return specs


@router.get("")
def get_api_directory() -> dict:
    config = load_config()
    host = _host_for_display(config.web_host)
    specs = _endpoint_specs(config, host)
    entries = [
        {"name": s["name"], "purpose": s["purpose"], "base_url": s["base_url"], "auth_hint": s["auth_hint"]}
        for s in specs
    ]
    return {"entries": entries}


def _probe_http(url: str, headers: dict | None = None) -> dict:
    """GET `url` once and classify the result as healthy/unhealthy.

    Healthy = a 2xx/3xx/4xx HTTP response actually came back (the service is
    up and answering — a 401/404 still proves the server is alive, which is
    all this board's "is the endpoint reachable" check cares about). Only a
    connection failure / timeout / DNS error counts as unhealthy. Never
    raises — every failure mode is mapped to a structured dict.
    """
    start = time.monotonic()
    try:
        with httpx.Client(timeout=_HEALTH_PROBE_TIMEOUT) as client:
            resp = client.get(url, headers=headers or {})
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        if resp.status_code in (401, 403):
            # Server is up and answering, but rejected our api-key. For a
            # vllm container this almost always means the on-disk compose
            # file baked in an old vllm_api_key (rendered before the key was
            # last changed) — actionable, so call it out explicitly rather
            # than the bland "可达". Counts as unhealthy because a real
            # client using the published key would also be rejected.
            detail = f"可达但密钥不匹配({resp.status_code})，请重新加载该模型以同步最新 vllm_api_key"
            return {"healthy": False, "status_code": resp.status_code,
                    "latency_ms": latency_ms, "detail": detail}
        return {
            "healthy": resp.status_code < 500,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "detail": "可达" if resp.status_code < 500 else f"服务端错误 {resp.status_code}",
        }
    except httpx.TimeoutException:
        return {"healthy": False, "status_code": None, "latency_ms": None, "detail": "请求超时"}
    except httpx.HTTPError as exc:
        return {"healthy": False, "status_code": None, "latency_ms": None, "detail": f"连接失败: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"healthy": False, "status_code": None, "latency_ms": None, "detail": str(exc)}


@router.get("/health-check")
def health_check() -> dict:
    """Actively probe every published endpoint and report live health.

    This is the "主动检测和验证API健康状态" action behind the 实时总览 ->
    API 发布 page's button: rather than only *listing* the endpoints
    (GET /api/api-directory does that), this one actually issues a real
    request to each and tells the user which are reachable right now. A
    loaded vllm model is probed at its OpenAI-compatible `/v1/models`
    (cheap, no inference, but proves the model server is serving); SearXNG
    is probed with a real JSON search through the configured proxy. The Web
    backend itself is reported healthy by definition (this handler running
    is the proof). Results are keyed by the same `name` values
    GET /api/api-directory returns so the frontend can line them up.
    """
    config = load_config()
    host = _host_for_display(config.web_host)
    specs = _endpoint_specs(config, host)

    results: list[dict] = []
    for spec in specs:
        probe = spec["probe"]
        kind = probe["kind"]
        if kind == "self":
            r = {"healthy": True, "status_code": 200, "latency_ms": 0.0,
                 "detail": "当前请求即由本服务处理，运行正常"}
            target = f"http://{host}:{config.web_port}/api/health"
        elif kind == "http":
            r = _probe_http(probe["url"], probe.get("headers"))
            target = probe["url"]
        elif kind == "searxng":
            sr = searxng_client.search(
                "healthcheck", config.searxng_url, proxy=config.searxng_proxy_url or None,
            )
            r = {"healthy": bool(sr.get("ok")),
                 "status_code": 200 if sr.get("ok") else None, "latency_ms": None,
                 "detail": "搜索接口正常返回JSON结果" if sr.get("ok")
                 else f"搜索失败: {sr.get('error') or '未知错误'}"}
            target = f"http://{host}:{config.searxng_port}/search"
        else:  # "unavailable" — a declared endpoint with no model loaded to back it
            r = {"healthy": False, "status_code": None, "latency_ms": None,
                 "detail": probe.get("detail", "不可用")}
            target = spec.get("base_url")
        results.append({"name": spec["name"], "target_url": target, **r})

    overall_healthy = all(r["healthy"] for r in results)
    return {"overall_healthy": overall_healthy, "checked_at": time.time(), "results": results}


def _is_embedding_loaded(model_name: str) -> bool:
    """Best-effort embedding classification for an already-loaded model
    name, without re-scanning the whole model directory on every call.

    Falls back to the model_scanner only if needed (cheap relative to the
    overall request, and scan_models() already degrades gracefully).
    """
    try:
        from core.config import load_config as _load_config
        from web import model_cache

        config = _load_config()
        for m in model_cache.get_models(config):
            if m.name == model_name:
                return m.is_embedding
    except Exception:  # pragma: no cover - defensive
        pass
    return False
