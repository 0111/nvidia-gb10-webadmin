"""GET /api/components, POST /api/components/{name}/start|stop|restart.

A "component" here covers every important network service this project
depends on, not just the models a user happens to have loaded right now —
the 实时总览 page's "组件状态" table is meant to answer "what's actually
running on this server", and a list that's empty whenever no model is
loaded fails that job. So `list_components()` always reports a fixed set
of infrastructure components (Web后端, 前端, SearXNG, 通用大模型容器,
嵌入大模型容器) using real `docker inspect`/pidfile state, in addition to
the registry-driven detail (container name, port, memory) for whichever
of the two vllm slots is currently loaded.
"""
from __future__ import annotations

import json
import logging
import subprocess

from fastapi import APIRouter, Depends, HTTPException, status

from core.config import load_config
from core.docker_helper import DockerComposeManager, SearxngManager
from core.process_manager import frontend_status as _frontend_proc_status
from core.process_manager import web_backend_status as _web_backend_proc_status
from web.auth import get_current_user
from web.schemas import ComponentActionResponse, ComponentListResponse, ComponentOut
from web.state import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/components", tags=["components"], dependencies=[Depends(get_current_user)])

# Fixed container names this project ever creates — see core/docker_helper.py
# (DockerComposeManager.render_compose) and deploy/searxng-compose.yml.
FIXED_CONTAINER_NAMES = {
    "general": "gb10-vllm-general",
    "embedding": "gb10-vllm-embedding",
    "searxng": "gb10-searxng",
}


def _container_states(container_names: list[str]) -> dict[str, str]:
    """Real container states via a single batched `docker inspect` call
    covering all requested names at once, independent of whether this
    backend process's in-memory registry knows about them.

    GET /api/components previously called `docker inspect` once per fixed
    container (3 separate subprocess spawns) plus one `docker stats` call —
    each subprocess spawn has measurable fixed overhead on this hardware,
    so batching the 3 inspects into 1 call removes 2 of those round trips.
    Docker tolerates unknown names mixed into a single `inspect` call: it
    just omits them from the JSON array output instead of failing the
    whole command, so any container that doesn't exist yet maps to
    "not_created" below rather than raising.
    """
    if not container_names:
        return {}
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.Name}}\t{{.State.Status}}", *container_names],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:  # pragma: no cover - defensive
        return {name: "unknown" for name in container_names}

    states: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, state = line.split("\t", 1)
        states[name.lstrip("/")] = state.strip() or "unknown"

    return {name: states.get(name, "not_created") for name in container_names}


def _manager() -> DockerComposeManager:
    config = load_config()
    return DockerComposeManager(model_root_host=config.model_root_dir, cuda_compat_dir=config.cuda_compat_dir)


def _parse_status(raw_stdout: str) -> tuple[str, float | None]:
    """Best-effort parse of `docker compose ps --format json` output.

    The format/shape varies across docker compose versions (single JSON
    object, JSON array, or NDJSON-one-object-per-line); we try a few
    strategies and fall back to "unknown" rather than raising.

    Memory usage is NOT derived from this output (docker compose ps does
    not report memory) — see `_docker_stats_mem_by_container()` below,
    which does a single batched `docker stats` call for all containers.
    """
    raw_stdout = (raw_stdout or "").strip()
    if not raw_stdout:
        return "unknown", None
    try:
        candidates = []
        try:
            parsed = json.loads(raw_stdout)
            candidates = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            for line in raw_stdout.splitlines():
                line = line.strip()
                if line:
                    candidates.append(json.loads(line))
        if not candidates:
            return "unknown", None
        state = str(candidates[0].get("State") or candidates[0].get("Status") or "unknown")
        return state, None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("解析 compose ps 输出失败: %s", exc)
        return "unknown", None


def _parse_mem_usage(mem_usage: str) -> float | None:
    """Parse a `docker stats --format {{.MemUsage}}` value like
    "512.3MiB / 119.6GiB" into a float number of MB. Returns None on any
    unparseable input rather than raising.
    """
    try:
        used_part = mem_usage.split("/")[0].strip()
        # Order matters: "GiB"/"MiB"/"KiB" all end with the literal "B",
        # so checking the bare "B" suffix first would always match first
        # and truncate the wrong number of characters (parsed as garbage,
        # silently swallowed by the ValueError below) — longest suffix
        # must be checked first.
        units = [("TiB", 1024.0 * 1024.0), ("GiB", 1024.0), ("MiB", 1.0),
                  ("KiB", 1 / 1024), ("B", 1 / (1024 * 1024))]
        for suffix, multiplier in units:
            if used_part.endswith(suffix):
                value = float(used_part[: -len(suffix)])
                return round(value * multiplier, 1)
        return round(float(used_part), 1)
    except (ValueError, IndexError):
        return None


def _docker_stats_mem_by_container(timeout: float = 15.0) -> dict[str, float]:
    """Single batched `docker stats --no-stream` call covering ALL running
    containers, returning {container_name: mem_usage_mb}.

    Deliberately not called once per component — a single invocation here
    keeps `GET /api/components` fast regardless of how many containers are
    registered. Any failure (docker not installed, no containers, command
    error) results in an empty dict rather than raising, so callers always
    get a usable (possibly all-None) memory column instead of a 500.
    """
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as exc:  # pragma: no cover - defensive
        logger.debug("docker stats 调用失败: %s", exc)
        return {}

    if result.returncode != 0:
        return {}

    mem_by_container: dict[str, float] = {}
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, mem_usage = line.split("\t", 1)
        mem_mb = _parse_mem_usage(mem_usage)
        if mem_mb is not None:
            mem_by_container[name.strip()] = mem_mb
    return mem_by_container


def _loaded_component_for_container(container_name: str) -> ComponentOut | None:
    """If a model is currently loaded into this fixed container slot,
    return the registry-driven detail (model name, port, memory)."""
    for name in registry.all_loaded_names():
        loaded = registry.get(name)
        if loaded and loaded.container_name == container_name:
            return ComponentOut(
                name=loaded.name, container_name=container_name, port=loaded.host_port,
                bind_host="0.0.0.0" if loaded.host_port else None,
                memory_usage_mb=None, status="running", detail="",
            )
    return None


@router.get("", response_model=ComponentListResponse)
def list_components() -> ComponentListResponse:
    config = load_config()
    mem_by_container = _docker_stats_mem_by_container()
    state_by_container = _container_states(list(FIXED_CONTAINER_NAMES.values()))
    components: list[ComponentOut] = []

    web_status = _web_backend_proc_status(config)
    components.append(ComponentOut(
        name="Web管理后台", container_name=None, port=config.web_port,
        # The web backend binds to config.web_host literally (uvicorn
        # --host), so report it verbatim — if an admin set 127.0.0.1 the UI
        # should honestly show "only local".
        bind_host=config.web_host,
        memory_usage_mb=None,
        status="running" if web_status["running"] else "stopped", detail="",
        manageable=False,
    ))

    frontend_status = _frontend_proc_status(config)
    components.append(ComponentOut(
        name="前端", container_name=None, port=config.frontend_port,
        # core.process_manager.start_frontend launches `vite preview --host
        # 0.0.0.0`, so the frontend is always LAN-reachable.
        bind_host="0.0.0.0",
        memory_usage_mb=None,
        status="running" if frontend_status["running"] else "stopped", detail="",
        manageable=False,
    ))

    searxng_container = FIXED_CONTAINER_NAMES["searxng"]
    searxng_state = state_by_container.get(searxng_container, "not_created")
    components.append(ComponentOut(
        name="SearXNG", container_name=searxng_container, port=config.searxng_port,
        # All docker-published ports (SearXNG + model containers) use the
        # compose template's "host_port:container_port" form, which docker
        # binds on 0.0.0.0 (every interface) — see core/docker_helper.py
        # COMPOSE_TEMPLATE and deploy/searxng-compose.yml.
        bind_host="0.0.0.0",
        memory_usage_mb=mem_by_container.get(searxng_container),
        status="running" if searxng_state == "running" else searxng_state, detail="",
        # Unlike the general/embedding model slots (managed via their
        # rendered per-model compose file) or the Web后端/前端 (self-managing
        # a restart of the very process answering this request would be
        # self-defeating), SearXNG has one fixed, always-the-same compose
        # file (deploy/searxng-compose.yml) — safe to expose start/stop/
        # restart here. component_action() below special-cases this name.
        manageable=True,
    ))

    for label, container_name in (("通用大模型容器", FIXED_CONTAINER_NAMES["general"]),
                                   ("嵌入式模型容器", FIXED_CONTAINER_NAMES["embedding"])):
        real_state = state_by_container.get(container_name, "not_created")
        loaded_component = _loaded_component_for_container(container_name)
        if loaded_component is not None:
            # `name` is kept as the actual loaded model name (not prefixed
            # with `label`) because POST /api/components/{name}/{action}
            # looks it up via registry.get(name) — the container_name
            # column already disambiguates which fixed slot this is.
            loaded_component.memory_usage_mb = mem_by_container.get(container_name)
            # The in-memory registry only records that a load was *started*
            # (compose up returned); it is NOT proof the vllm container is
            # alive. A model whose container crashed during weight loading
            # (bad params / missing shard / OOM / vllm error-exit) stays in
            # the registry, so we must trust the REAL docker state here
            # instead of hardcoding "running" — otherwise the UI keeps
            # claiming a dead model is loaded and running. This is what makes
            # the page "及时发现" a failed load.
            if real_state == "running":
                loaded_component.status = "running"
            elif real_state in ("exited", "dead"):
                loaded_component.status = "failed"
                loaded_component.detail = f"容器已退出({real_state})，疑似加载失败，请查看组件日志"
            elif real_state == "restarting":
                loaded_component.status = "failed"
                loaded_component.detail = "容器反复重启(crash loop)，加载失败，请查看组件日志"
            elif real_state in ("not_created", "unknown"):
                loaded_component.status = "failed"
                loaded_component.detail = "容器已不存在，加载失败或已被外部移除"
            else:  # created / paused / etc.
                loaded_component.status = real_state
                loaded_component.detail = f"容器状态 {real_state}"
            components.append(loaded_component)
            continue
        components.append(ComponentOut(
            name=label, container_name=container_name, port=None,
            memory_usage_mb=mem_by_container.get(container_name),
            status=("unloaded" if real_state == "not_created"
                    else "failed" if real_state in ("exited", "dead") else real_state),
            detail=("" if real_state in ("not_created", "running")
                    else f"容器状态 {real_state}，疑似加载失败，请查看组件日志"),
            manageable=False,
        ))

    return ComponentListResponse(components=components)


@router.post("/{name}/{action}", response_model=ComponentActionResponse)
def component_action(name: str, action: str) -> ComponentActionResponse:
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未知操作: {action}")

    if name == "SearXNG":
        searxng_manager = SearxngManager()
        fn = {"start": searxng_manager.start, "stop": searxng_manager.stop}.get(action)
        if fn is None:
            # SearxngManager has no dedicated restart(); compose restart on
            # its fixed compose file does the same job without needing a
            # third method that duplicates start()+stop().
            result = _manager()._run_compose(searxng_manager.compose_path, ["restart"], timeout=120)
        else:
            result = fn()
        return ComponentActionResponse(
            name=name, action=action,  # type: ignore[arg-type]
            ok=result.ok,
            message="操作成功" if result.ok else (result.error or result.stderr or "操作失败"),
            command_result={
                "ok": result.ok, "command": result.command, "stdout": result.stdout,
                "stderr": result.stderr, "returncode": result.returncode, "error": result.error,
            },
        )

    loaded = registry.get(name)
    if loaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail=f"未找到组件: {name}（尚未通过本系统加载）")

    docker_manager = _manager()
    fn = {"start": docker_manager.start, "stop": docker_manager.stop, "restart": docker_manager.restart}[action]
    result = fn(loaded.compose_path)

    return ComponentActionResponse(
        name=name,
        action=action,  # type: ignore[arg-type]
        ok=result.ok,
        message="操作成功" if result.ok else (result.error or result.stderr or "操作失败"),
        command_result={
            "ok": result.ok, "command": result.command, "stdout": result.stdout,
            "stderr": result.stderr, "returncode": result.returncode, "error": result.error,
        },
    )
