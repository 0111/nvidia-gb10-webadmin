"""统一对外 API 网关：在 Web 后端端口(默认8000)上暴露 OpenAI 与 Claude
兼容接口，转发到当前已加载的 vllm 模型容器。

为什么需要这一层（而不是让客户端直接连 8001/8002）：
  - 单一稳定入口：外部客户端只需记住 `http://<server>:8000/v1/...`，无需关心
    某个模型当前监听在 8001 还是 8002、是否换过端口。
  - 按 body 里的 `model` 字段自动路由到对应容器（找不到精确匹配时，按接口
    类型回退到唯一已加载的通用/嵌入模型）。
  - 统一鉴权：用项目的 `vllm_api_key` 校验外部请求（OpenAI 风格
    `Authorization: Bearer <key>` 或 Claude 风格 `x-api-key: <key>` 均可），
    再用容器自己的 key 转发到上游——对外口径统一，且修正了 vllm 原生
    `/v1/messages` 只认 Bearer 不认 x-api-key 的别扭之处（这里两种都接受）。
  - 支持流式(SSE)：`"stream": true` 时透传上游分块响应。

暴露的路由（与 vllm 0.22 容器实际路由一致）：
  OpenAI:  GET /v1/models, POST /v1/chat/completions, /v1/completions, /v1/embeddings
  Claude:  POST /v1/messages, /v1/messages/count_tokens

注意：本路由 *不* 挂全局 JWT 依赖（那是给管理前端用的），它是面向外部
程序调用的数据面接口，用 api-key 鉴权。
"""
from __future__ import annotations

import json
import asyncio
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from core.config import load_config
from web.anthropic_compat import sanitize_anthropic_body
from web.state import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-claude-gateway"])

GATEWAY_TIMEOUT_SECONDS = 300.0
# Transient upstream connection failures (httpx ReadError / RemoteProtocolError
# "Server disconnected without sending a response") occasionally happen when
# opening the stream to the local vllm container. They surface to clients like
# OpenWebUI as `TransferEncodingError: Not enough data to satisfy transfer
# length header` because the chunked SSE body gets cut before the terminating
# chunk. These errors occur at connect/send time (before any body byte), so
# retrying the open is safe. Errors caught here (subclasses of TransportError).
GATEWAY_STREAM_OPEN_RETRIES = 3
_STREAM_RETRYABLE = (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectTimeout)

GENERAL_CONTAINER = "gb10-vllm-general"
EMBEDDING_CONTAINER = "gb10-vllm-embedding"


def _check_api_key(authorization: str | None, x_api_key: str | None) -> None:
    """Validate the caller's key against config.vllm_api_key.

    Accepts both OpenAI-style `Authorization: Bearer <key>` and Claude-style
    `x-api-key: <key>`, so a client configured either way works against the
    same gateway. Raises 401 on mismatch/absence.
    """
    config = load_config()
    expected = config.vllm_api_key
    if not expected:
        return  # no key configured → open (shouldn't happen; key is auto-generated)

    presented = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_api_key:
        presented = x_api_key.strip()

    if presented != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权：请在 Authorization: Bearer <key> 或 x-api-key 头中提供正确的 API 密钥",
        )


def _port_for_container(container_name: str) -> int | None:
    for name in registry.all_loaded_names():
        loaded = registry.get(name)
        if loaded and loaded.container_name == container_name and loaded.host_port:
            return loaded.host_port
    return None


def _resolve_upstream(model_name: str | None, prefer_embedding: bool) -> int:
    """Pick the upstream vllm port for this request.

    Priority: exact match on the requested model name (so loading a general
    + embedding model and addressing each by name both work) → else the
    single loaded model of the appropriate kind (embedding for /v1/embeddings,
    general otherwise). Raises 404 with a clear message if nothing suitable
    is loaded.
    """
    if model_name:
        loaded = registry.get(model_name)
        if loaded and loaded.host_port:
            return loaded.host_port

    fallback_container = EMBEDDING_CONTAINER if prefer_embedding else GENERAL_CONTAINER
    port = _port_for_container(fallback_container)
    if port:
        return port

    kind = "嵌入" if prefer_embedding else "通用"
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"未找到可服务的{kind}模型"
            + (f"（请求的 model=「{model_name}」未加载）" if model_name else "")
            + "。请先在「模型配置」页加载对应模型。"
        ),
    )


def _sanitize_anthropic_body(raw_body: bytes) -> bytes:
    """Raw-bytes adapter over web.anthropic_compat.sanitize_anthropic_body:
    move stray `role:"system"` entries in an Anthropic /v1/messages body into
    the top-level `system` field (vllm's native /v1/messages 400s otherwise).
    Returns the original bytes unchanged when there's nothing to fix."""
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return raw_body
    new_body, changed = sanitize_anthropic_body(body)
    if not changed:
        return raw_body
    return json.dumps(new_body, ensure_ascii=False).encode("utf-8")


async def _forward(request: Request, path: str, prefer_embedding: bool,
                   body_transform=None) -> object:
    """Forward the request body to the resolved upstream vllm container,
    streaming the response back when the client asked for stream=true.

    `body_transform`: optional callable(bytes)->bytes applied to the raw body
    before forwarding (used to sanitize Anthropic /v1/messages bodies)."""
    raw_body = await request.body()
    if body_transform is not None and raw_body:
        raw_body = body_transform(raw_body)
    model_name: str | None = None
    stream = False
    if raw_body:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                model_name = parsed.get("model")
                stream = bool(parsed.get("stream", False))
        except (json.JSONDecodeError, ValueError):
            pass  # forward as-is; upstream will validate

    port = _resolve_upstream(model_name, prefer_embedding)
    config = load_config()
    url = f"http://127.0.0.1:{port}{path}"
    # Always present the container's own key upstream as Bearer (vllm only
    # accepts Bearer), regardless of which header the external client used.
    headers = {"Content-Type": "application/json"}
    if config.vllm_api_key:
        headers["Authorization"] = f"Bearer {config.vllm_api_key}"

    if stream:
        async def event_stream():
            async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT_SECONDS) as client:
                for attempt in range(GATEWAY_STREAM_OPEN_RETRIES):
                    started = False  # have we yielded any body byte yet?
                    try:
                        async with client.stream("POST", url, content=raw_body, headers=headers) as resp:
                            async for chunk in resp.aiter_raw():
                                started = True
                                yield chunk
                        return  # upstream stream completed normally
                    except _STREAM_RETRYABLE as exc:
                        # Only safe to retry if nothing was sent to the client
                        # yet — otherwise we'd duplicate/corrupt the SSE stream.
                        if not started and attempt < GATEWAY_STREAM_OPEN_RETRIES - 1:
                            logger.info("网关流式转发瞬时失败，重试 attempt=%d url=%s: %r", attempt, url, exc)
                            await asyncio.sleep(0.3 * (attempt + 1))
                            continue
                        # Retries exhausted, or failure mid-body: terminate the
                        # SSE stream CLEANLY (proper final chunk) with an error
                        # event, so the client sees an error rather than a
                        # truncated body ("Not enough data to satisfy transfer
                        # length header").
                        logger.warning("网关流式转发中断 url=%s attempt=%d started=%s: %r",
                                       url, attempt, started, exc)
                        err = json.dumps({"error": {
                            "message": f"上游模型流式响应中断: {type(exc).__name__}",
                            "type": "upstream_stream_error"}})
                        yield f"data: {err}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, content=raw_body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("网关转发失败 url=%s: %r", url, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"上游模型服务转发失败: {exc}")

    media_type = resp.headers.get("content-type", "application/json")
    return StreamingResponse(iter([resp.content]), status_code=resp.status_code, media_type=media_type)


@router.get("/models")
def gateway_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> JSONResponse:
    """OpenAI /v1/models — list models currently loaded & servable through
    this gateway (built from the in-memory registry, no upstream call)."""
    _check_api_key(authorization, x_api_key)
    data = []
    for name in registry.all_loaded_names():
        loaded = registry.get(name)
        if loaded and loaded.host_port:
            data.append({"id": name, "object": "model", "owned_by": "vllm"})
    return JSONResponse({"object": "list", "data": data})


@router.post("/chat/completions")
async def gateway_chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> object:
    _check_api_key(authorization, x_api_key)
    return await _forward(request, "/v1/chat/completions", prefer_embedding=False)


@router.post("/completions")
async def gateway_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> object:
    _check_api_key(authorization, x_api_key)
    return await _forward(request, "/v1/completions", prefer_embedding=False)


@router.post("/embeddings")
async def gateway_embeddings(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> object:
    _check_api_key(authorization, x_api_key)
    return await _forward(request, "/v1/embeddings", prefer_embedding=True)


@router.post("/messages")
async def gateway_claude_messages(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> object:
    """Claude (Anthropic) Messages API — forwarded to vllm's native
    /v1/messages on the general model container, after sanitizing any stray
    system-role messages into the top-level `system` field."""
    _check_api_key(authorization, x_api_key)
    return await _forward(request, "/v1/messages", prefer_embedding=False,
                          body_transform=_sanitize_anthropic_body)


@router.post("/messages/count_tokens")
async def gateway_claude_count_tokens(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> object:
    _check_api_key(authorization, x_api_key)
    return await _forward(request, "/v1/messages/count_tokens", prefer_embedding=False,
                          body_transform=_sanitize_anthropic_body)
