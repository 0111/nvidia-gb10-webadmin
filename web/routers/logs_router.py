"""GET /api/logs/{component}?lines=200 — tail logs for a component.

`component` accepts several identifier shapes, tried in order:
  - "searxng"  -> SearXNG container logs
  - "web"      -> this backend's own log file (data/logs/web.log)
  - "frontend" -> the `vite preview` process log file (data/logs/frontend.log)
  - any real container name (e.g. "gb10-vllm-general") -> `docker logs`
    directly, independent of the in-memory load registry — this is what
    makes log fetching work for the *fixed* component rows the 实时总览/
    组件日志 pages always show (Web/前端/SearXNG/通用模型容器/嵌入模型容器),
    not just whatever this backend process happens to remember loading.
  - a loaded model's name (matched via web.state.registry, kept for
    backwards compatibility with any caller that still passes a model name
    instead of its container name)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from core.config import load_config
from core.docker_helper import DockerComposeManager, SearxngManager, container_logs
from web.auth import get_current_user
from web.schemas import LogsResponse
from web.state import registry

router = APIRouter(prefix="/api/logs", tags=["logs"], dependencies=[Depends(get_current_user)])

SEARXNG_ALIAS = "searxng"
WEB_ALIAS = "web"
FRONTEND_ALIAS = "frontend"
FIXED_CONTAINER_PREFIXES = ("gb10-",)


def _tail_log_file(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return f"读取日志文件失败: {exc}"
    return "\n".join(text.splitlines()[-lines:])


def resolve_logs(component: str, lines: int = 200) -> tuple[str | None, bool]:
    """Return (content, found) for a component's logs.

    Shared by the REST endpoint and the WebSocket log streamer (ws_router),
    so both resolve identifiers the same way. `found` is False only when the
    identifier matches nothing recognizable (REST turns that into a 404; the
    WS streamer just stops). Never raises.
    """
    config = load_config()

    if component == SEARXNG_ALIAS:
        result = SearxngManager().logs(tail=lines)
        return (result.stdout if result.ok else (result.error or result.stderr or "")), True

    if component == WEB_ALIAS:
        return _tail_log_file(Path(config.data_dir) / "logs" / "web.log", lines), True

    if component == FRONTEND_ALIAS:
        return _tail_log_file(Path(config.data_dir) / "logs" / "frontend.log", lines), True

    if component.startswith(FIXED_CONTAINER_PREFIXES):
        result = container_logs(component, tail=lines)
        return (result.stdout if result.ok else (result.error or result.stderr or "")), True

    loaded = registry.get(component)
    if loaded is None:
        return None, False

    docker_manager = DockerComposeManager(model_root_host=config.model_root_dir,
                                           cuda_compat_dir=config.cuda_compat_dir)
    result = docker_manager.logs(loaded.compose_path, tail=lines)
    return (result.stdout if result.ok else (result.error or result.stderr or "")), True


@router.get("/{component}", response_model=LogsResponse)
def get_logs(component: str, lines: int = 200) -> LogsResponse:
    content, found = resolve_logs(component, lines)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"未找到组件: {component}（可用标识：'{WEB_ALIAS}' / '{FRONTEND_ALIAS}' / "
                f"'{SEARXNG_ALIAS}' / 任意 gb10- 开头的容器名 / 已加载的模型名）"
            ),
        )
    return LogsResponse(component=component, lines=lines, content=content or "")
