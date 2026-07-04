"""POST /api/debug/chat — forward a debug request to a *real* loaded model.

Design: the frontend lets the user pick a "display format" (OpenAI or Claude
messages shape). The request is forwarded to the matching NATIVE vllm 0.22.1
endpoint so the displayed HTTP packet pair is the genuine thing, not a
translation:
  - api_format == "openai" → build an OpenAI body, POST /v1/chat/completions.
  - api_format == "claude" → build a canonical Anthropic body, POST the
    native /v1/messages, and return its response verbatim.

vllm 0.22.1 natively serves BOTH APIs (/v1/chat/completions and /v1/messages),
so there is no Claude↔OpenAI translation layer here anymore — an earlier
version translated Claude→OpenAI and faked a Claude response envelope, which
both duplicated what vllm does natively and showed the user a response that
wasn't the real /v1/messages output.

The real outgoing request/response (method, url, headers, body, status code)
are returned verbatim so the frontend can render the raw HTTP packet pair per
the API-调试 requirement. Any network/timeout error is caught and returned as
a structured (not 500) response.
"""
from __future__ import annotations

import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from core.config import load_config
from pydantic import BaseModel

from web.anthropic_compat import sanitize_anthropic_body
from web.auth import get_current_user
from web.schemas import DebugChatRequest, DebugChatResponse
from web.state import registry


class DebugEmbeddingRequest(BaseModel):
    model_name: str
    input: str

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"], dependencies=[Depends(get_current_user)])

DEFAULT_TIMEOUT_SECONDS = 120.0


def _resolve_base_url(model_name: str) -> str:
    """Look up the real listening address for a loaded model via the
    process-wide ModelLoadRegistry (web/state.py), which tracks host_port
    per loaded model."""
    loaded = registry.get(model_name)
    if loaded is None or not loaded.host_port:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"模型 {model_name} 当前未加载或未记录监听端口，无法转发调试请求",
        )
    return f"http://127.0.0.1:{loaded.host_port}"


def _build_openai_payload(payload: DebugChatRequest) -> dict:
    """OpenAI /v1/chat/completions body from the unified DebugChatRequest."""
    messages: list[dict] = []
    if payload.system:
        messages.append({"role": "system", "content": payload.system})
    messages.append({"role": "user", "content": payload.prompt})

    body = {
        "model": payload.model_name,
        "messages": messages,
        "max_tokens": payload.max_tokens,
        "temperature": payload.temperature,
    }
    extra = dict(payload.extra)
    extra.pop("messages", None)
    body.update(extra)
    return body


def _build_anthropic_payload(payload: DebugChatRequest) -> dict:
    """Canonical Anthropic /v1/messages body from the unified DebugChatRequest.

    The system prompt goes to the top-level `system` field (vllm's native
    /v1/messages rejects a system *message role* with a 400). A full
    Claude-shaped `messages` list may arrive via `extra.messages`; otherwise
    the single `prompt` becomes one user turn. Any stray system-role messages
    are folded into top-level `system` via the shared anthropic_compat helper.
    """
    body: dict = {
        "model": payload.model_name,
        "max_tokens": payload.max_tokens,
        "temperature": payload.temperature,
    }
    if payload.system:
        body["system"] = payload.system

    claude_messages = payload.extra.get("messages")
    if isinstance(claude_messages, list) and claude_messages:
        body["messages"] = claude_messages
    else:
        body["messages"] = [{"role": "user", "content": payload.prompt}]

    extra = dict(payload.extra)
    extra.pop("messages", None)
    body.update(extra)

    sanitized, _changed = sanitize_anthropic_body(body)
    return sanitized


@router.post("/chat", response_model=DebugChatResponse)
async def debug_chat(payload: DebugChatRequest) -> DebugChatResponse:
    base_url = _resolve_base_url(payload.model_name)
    # Forward to the matching NATIVE vllm endpoint for the chosen format, so
    # the displayed packet is the real thing (no Claude↔OpenAI translation).
    if payload.api_format == "claude":
        path = "/v1/messages"
        request_payload = _build_anthropic_payload(payload)
    else:
        path = "/v1/chat/completions"
        request_payload = _build_openai_payload(payload)
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}

    # Every vllm container is launched with --api-key (see core.config.
    # AppConfig.vllm_api_key / core.docker_helper.render_compose), so a
    # forwarded request with no auth header at all always gets a 401
    # "Unauthorized" back regardless of the prompt/model being otherwise
    # correct — confirmed live (curl reproduced the exact 401 body the
    # user reported). vllm 0.22 accepts either OpenAI-style
    # `Authorization: Bearer <key>` or Anthropic-style `x-api-key: <key>`,
    # so send both headers unconditionally rather than branching on
    # api_format — both being present costs nothing and removes any risk of
    # picking the "wrong" one for a given endpoint/vllm build.
    config = load_config()
    if config.vllm_api_key:
        headers["Authorization"] = f"Bearer {config.vllm_api_key}"
        headers["x-api-key"] = config.vllm_api_key

    request_packet = {
        "method": "POST",
        "url": url,
        "headers": headers,
        "body": request_payload,
    }

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            start = time.monotonic()
            response = await client.post(url, json=request_payload, headers=headers)
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)

        # Return the upstream response verbatim — for "claude" this is the
        # genuine native /v1/messages body, for "openai" the real
        # /v1/chat/completions body. No translation/faking.
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = {"raw_text": response.text}

        response_packet = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response_payload,
            "elapsed_ms": elapsed_ms,
        }

        return DebugChatResponse(
            request_payload=request_packet,
            status_code=response.status_code,
            response_payload=response_packet,
        )
    except httpx.HTTPError as exc:
        logger.warning("调试转发请求失败 url=%s: %r", url, exc, exc_info=True)
        return DebugChatResponse(
            request_payload=request_packet,
            status_code=None,
            response_payload=None,
            error=f"转发请求失败: {type(exc).__name__}: {exc}",
        )


@router.post("/embedding", response_model=DebugChatResponse)
async def debug_embedding(payload: DebugEmbeddingRequest) -> DebugChatResponse:
    """Forward an embeddings request to a loaded embedding model's
    OpenAI-compatible /v1/embeddings endpoint.

    Chat completions are meaningless for an embedding/reranker model (it has
    no generative head) — vllm returns an error if you POST one to
    /v1/chat/completions against an embedding model. So the API调试 page,
    when the selected model is an embedding model, calls THIS instead, which
    posts {input} to /v1/embeddings and returns the same request/response
    packet shape as debug_chat. To keep the displayed response readable, the
    (potentially thousands-of-floats) embedding vector is summarized to its
    dimension + first few values rather than dumped in full.
    """
    base_url = _resolve_base_url(payload.model_name)
    url = base_url.rstrip("/") + "/v1/embeddings"
    body = {"model": payload.model_name, "input": payload.input}
    headers = {"Content-Type": "application/json"}
    config = load_config()
    if config.vllm_api_key:
        headers["Authorization"] = f"Bearer {config.vllm_api_key}"
        headers["x-api-key"] = config.vllm_api_key

    request_packet = {"method": "POST", "url": url, "headers": headers, "body": body}

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            start = time.monotonic()
            response = await client.post(url, json=body, headers=headers)
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)

        try:
            raw = response.json()
        except ValueError:
            raw = {"raw_text": response.text}

        # Summarize the vector(s) so the 响应包 stays human-readable.
        summarized = raw
        if isinstance(raw, dict) and isinstance(raw.get("data"), list):
            summarized = dict(raw)
            new_data = []
            for item in raw["data"]:
                if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                    vec = item["embedding"]
                    new_data.append({
                        **{k: v for k, v in item.items() if k != "embedding"},
                        "embedding_dim": len(vec),
                        "embedding_preview": [round(float(x), 6) for x in vec[:8]] + (["..."] if len(vec) > 8 else []),
                    })
                else:
                    new_data.append(item)
            summarized["data"] = new_data

        response_packet = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": summarized,
            "elapsed_ms": elapsed_ms,
        }
        return DebugChatResponse(
            request_payload=request_packet,
            status_code=response.status_code,
            response_payload=response_packet,
        )
    except httpx.HTTPError as exc:
        logger.warning("嵌入调试转发失败 url=%s: %r", url, exc, exc_info=True)
        return DebugChatResponse(
            request_payload=request_packet,
            status_code=None,
            response_payload=None,
            error=f"转发请求失败: {type(exc).__name__}: {exc}",
        )
