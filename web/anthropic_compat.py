"""Shared Anthropic (Claude) Messages-API request helpers.

vllm 0.22.1 natively serves the Anthropic Messages API at /v1/messages, but
its request validation is strict: `messages[]` may only contain
'user'/'assistant' roles — the system prompt must be the top-level `system`
field. Some clients (notably the Claude CLI) instead put a system turn
inside `messages`, which makes vllm reject the whole request with a 400
`literal_error` on messages[].role.

Both the outward gateway (web/routers/gateway_router.py) and the API-debug
forwarder (web/routers/debug_router.py) talk to that native endpoint, so the
"move stray system messages to top-level system" fix-up lives here once
instead of being duplicated in both.
"""
from __future__ import annotations


def flatten_anthropic_content(content) -> str:
    """Flatten an Anthropic message `content` (a string, or a list of typed
    blocks) into plain text, keeping only text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def sanitize_anthropic_body(body: dict) -> tuple[dict, bool]:
    """Move any stray `role:"system"` entries out of the Anthropic `messages`
    array into the top-level `system` field.

    Returns (possibly-new body dict, changed?). When nothing needs fixing the
    original dict is returned with changed=False so callers can preserve the
    raw bytes untouched.
    """
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return body, False

    sys_parts: list[str] = []
    existing = body.get("system")
    if isinstance(existing, str):
        sys_parts.append(existing)
    elif isinstance(existing, list):
        sys_parts.append(flatten_anthropic_content(existing))

    kept: list = []
    changed = False
    for m in body["messages"]:
        if isinstance(m, dict) and m.get("role") == "system":
            sys_parts.append(flatten_anthropic_content(m.get("content")))
            changed = True
        else:
            kept.append(m)

    if not changed:
        return body, False

    new_body = dict(body)
    new_body["messages"] = kept
    merged = "\n\n".join(p for p in sys_parts if p and p.strip())
    if merged:
        new_body["system"] = merged
    elif "system" in new_body:
        del new_body["system"]
    return new_body, True
