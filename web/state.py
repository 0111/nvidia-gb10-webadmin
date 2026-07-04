"""Process-wide in-memory state for the Web backend.

This is intentionally tiny: it tracks which models currently have a
compose file rendered/started by this backend process, so /api/overview
and /api/models can report load_status without re-shelling to `docker
compose ps` on every request. It is NOT a source of truth for whether a
container is actually healthy — `components_router`/`docker_helper.status`
remains authoritative for that. Lost on process restart, which is
acceptable since "no persistence, must be manually started" is a stated
project requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock


@dataclass
class LoadedModel:
    name: str
    engine: str
    compose_path: str
    container_name: str
    host_port: int | None = None


class ModelLoadRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._loaded: dict[str, LoadedModel] = {}

    def mark_loading(self, name: str, engine: str, compose_path: str, container_name: str,
                      host_port: int | None = None) -> None:
        with self._lock:
            self._loaded[name] = LoadedModel(
                name=name, engine=engine, compose_path=compose_path,
                container_name=container_name, host_port=host_port,
            )

    def mark_unloaded(self, name: str) -> None:
        with self._lock:
            self._loaded.pop(name, None)

    def get(self, name: str) -> LoadedModel | None:
        with self._lock:
            return self._loaded.get(name)

    def all_loaded_names(self) -> list[str]:
        with self._lock:
            return list(self._loaded.keys())


registry = ModelLoadRegistry()


def real_load_status() -> dict[str, str]:
    """Real per-model load status for everything the registry thinks is loaded,
    resolved from the actual container State.Status:
      'running' — container up (model loaded, or still loading weights)
      'failed'  — container exited / dead / restarting / missing (crashed load)

    The in-memory registry only records that a load was *started* (`docker
    compose up` returned); a container that crashes seconds/minutes later still
    lingers in all_loaded_names(). So any endpoint that reports load state to
    the UI must use THIS — otherwise a dead model shows as "已加载/卸载". (The
    组件状态 page already reads real docker state; this brings 总览 and the
    模型列表 in line.)
    """
    import subprocess

    result: dict[str, str] = {}
    for name in registry.all_loaded_names():
        loaded = registry.get(name)
        container = loaded.container_name if loaded else None
        state = ""
        if container:
            try:
                proc = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Status}}", container],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                state = (proc.stdout or "").strip() if proc.returncode == 0 else ""
            except Exception:  # pragma: no cover - defensive
                state = ""
        result[name] = "running" if state == "running" else "failed"
    return result


def compose_dir(data_dir: str) -> Path:
    """Where rendered docker-compose files for loaded models are written."""
    path = Path(data_dir) / "compose"
    path.mkdir(parents=True, exist_ok=True)
    return path


def reconcile_from_disk(data_dir: str) -> None:
    """Rebuild the in-memory registry from real container state on disk.

    The registry only remembers what *this process* loaded via its own
    /api/models/{name}/load call. If a model was instead started by the
    CLI (`python -m cli.main start`, which drives the same rendered
    `data/compose/*.compose.yml` files directly) or was already running
    before this web process started, the registry would otherwise stay
    empty and report "nothing loaded" even though containers are up —
    defeating the project's "CLI and Web share the same source of truth"
    design. Called once at FastAPI startup; subsequent load/unload calls
    keep the registry in sync as before.

    IMPORTANT: `gb10-vllm-general` / `gb10-vllm-embedding` are *fixed*
    container names shared by every general/embedding vllm model (only one
    of each can run at a time — that's what the fixed host port implies
    too). Stale `data/compose/*.compose.yml` files can accumulate on disk
    from a previous load attempt that failed or was superseded (the
    compose file is written before `docker compose up` runs, so a failed
    or replaced load still leaves a file behind). Matching by *filename*
    is therefore unreliable when multiple files target the same container
    name — only the container's actual running command (read via `docker
    inspect`) tells you which model is really being served.
    """
    for container_name in ("gb10-vllm-general", "gb10-vllm-embedding"):
        served_name = _served_model_name_for_container(container_name)
        if served_name is None:
            continue
        host_port = _published_host_port(container_name)
        compose_file = compose_dir(data_dir) / f"{served_name}.compose.yml"
        registry.mark_loading(served_name, "vllm", str(compose_file), container_name, host_port)


def current_model_in_container(container_name: str) -> str | None:
    """Public, truth-from-docker helper: the `--served-model-name` of the
    model actually running in a fixed container slot right now, or None if
    that slot is empty.

    Reads the live container args via `docker inspect` rather than trusting
    the in-memory registry — the registry can lag behind reality (e.g. a
    container started by the CLI after this web process booted, or a model
    that crashed without an unload call). Used to enforce the "only one
    general + one embedding model at a time" rule in models_router.load:
    the fixed `gb10-vllm-general` / `gb10-vllm-embedding` container names
    already make it physically true that loading a second model into the
    same slot would clobber the first, so the load endpoint refuses up
    front and tells the user to unload the current model instead of
    silently replacing it.
    """
    return _served_model_name_for_container(container_name)


def _is_container_running(container_name: str) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except Exception:
        return False


def _served_model_name_for_container(container_name: str) -> str | None:
    """Read the real `--served-model-name` value from a running container's
    actual launch args, rather than trusting any on-disk compose filename."""
    import subprocess

    if not _is_container_running(container_name):
        return None
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Args}}", container_name],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            return None
        import json

        args = json.loads(result.stdout)
        if "--served-model-name" in args:
            idx = args.index("--served-model-name")
            return args[idx + 1]
    except Exception:
        pass
    return None


def _published_host_port(container_name: str) -> int | None:
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "inspect", "-f",
             "{{(index (index .NetworkSettings.Ports \"8000/tcp\") 0).HostPort}}", container_name],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except Exception:
        pass
    return None
