"""Process/component orchestration used by `cli/main.py` start/stop/restart/clean.

This module owns the *real* start/stop/clean logic so cli/main.py stays a
thin click wrapper. It manages three kinds of "project components":

  (a) the Web backend (a plain `uvicorn web.main:app` process, tracked via
      a pidfile at `<data_dir>/web.pid` — there is no systemd/supervisor in
      scope for this project, so a pidfile + SIGTERM is the whole lifecycle
      story);
  (b) the SearXNG container (via core.docker_helper.SearxngManager, fixed
      compose file at deploy/searxng-compose.yml);
  (c) every rendered model compose file under `<data_dir>/compose/*.compose.yml`
      (all models — safetensors and GGUF alike — are vllm containers, so
      there is no separate shared-container component to special-case here).

All functions return small structured result dicts (never raise) so the
CLI can print a clean, readable summary instead of a stack trace.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from core.config import AppConfig
from core.docker_helper import DockerComposeManager, SearxngManager, ensure_vllm_image, image_exists

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _web_python_executable() -> str:
    """Python interpreter to launch the uvicorn web backend with.

    `cli/main.py` may be invoked as bare `python3 -m cli.main start` (NOT
    from inside the project's virtualenv) — that's the natural muscle memory
    on the server. In that case `sys.executable` is the *system* python
    (/usr/bin/python3), which does NOT have uvicorn/fastapi installed, so
    the spawned `sys.executable -m uvicorn ...` exits immediately with
    "No module named uvicorn" and `cli start` reports the web backend as
    failed even though everything else is fine (observed live on the
    server). Prefer the project's own `.venv/bin/python` when it exists so
    the web backend always starts with the right dependencies regardless of
    how the CLI itself was launched; fall back to sys.executable otherwise
    (e.g. the venv really is active, or a non-venv install on a dev box).
    """
    venv_python = _project_root() / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def web_pid_path(config: AppConfig) -> Path:
    return Path(config.data_dir) / "web.pid"


def _compose_dir(config: AppConfig) -> Path:
    path = Path(config.data_dir) / "compose"
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_compose_files(config: AppConfig) -> list[Path]:
    """All rendered model compose files."""
    return sorted(_compose_dir(config).glob("*.compose.yml"))


# ---------------------------------------------------------------------------
# Web backend process (pidfile-managed)
# ---------------------------------------------------------------------------

def _pid_is_running(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            # Best-effort on Windows dev boxes; the real target is Linux
            # (ARM64 DGX Spark), where os.kill(pid, 0) works as expected.
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False
    except Exception:  # pragma: no cover - defensive
        return False


def _kill_process_tree(pid: int) -> None:
    """SIGTERM the whole process group rooted at `pid`, not just `pid`.

    Both the web backend (`python -m uvicorn`) and the frontend (`npx vite
    preview`) are launched with `start_new_session=True`, which makes the
    spawned process a session/group leader whose pgid equals its own pid.
    `npx` in particular is a thin wrapper that forks a *child* node process
    to actually run `vite preview` — SIGTERM'ing only the recorded npx pid
    leaves that child running and still serving traffic (observed on the
    real server: `npx` pid disappears but the spawned `node .../vite`
    process survives the "stop" and keeps answering on the port). Killing
    the whole group reaches the child too.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


def web_backend_status(config: AppConfig) -> dict:
    """Returns {running, pid} based on the pidfile (not port probing)."""
    pid_path = web_pid_path(config)
    if not pid_path.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return {"running": False, "pid": None}
    if _pid_is_running(pid):
        return {"running": True, "pid": pid}
    return {"running": False, "pid": pid}


def start_web_backend(config: AppConfig) -> dict:
    """Start `uvicorn web.main:app` in the background, detached from this
    CLI process, and record its pid in `<data_dir>/web.pid`.

    Returns {ok, message, pid, already_running}. Never raises — any
    failure to spawn the process is captured and reported in `message`.
    """
    status = web_backend_status(config)
    if status["running"]:
        return {"ok": True, "message": f"Web 后端已在运行 (pid={status['pid']})",
                "pid": status["pid"], "already_running": True}

    root = _project_root()
    cmd = [_web_python_executable(), "-m", "uvicorn", "web.main:app",
           "--host", config.web_host, "--port", str(config.web_port)]

    log_dir = Path(config.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "web.log"

    try:
        log_file = open(log_path, "a", encoding="utf-8")
        popen_kwargs: dict = {
            "cwd": str(root),
            "stdout": log_file,
            "stderr": log_file,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: closest Windows
            # equivalent of "nohup ... &" — survives the parent CLI exiting.
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            # True nohup-style detach on Linux (the real ARM64 target):
            # new session so SIGHUP/SIGINT to the CLI's process group does
            # not propagate to the uvicorn process.
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(cmd, **popen_kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "message": f"启动 Web 后端失败: {exc}", "pid": None, "already_running": False}

    web_pid_path(config).parent.mkdir(parents=True, exist_ok=True)
    web_pid_path(config).write_text(str(process.pid), encoding="utf-8")

    # Brief grace period so an immediate crash (e.g. import error, port
    # already bound by something else) is visible in the CLI output rather
    # than silently reporting "started" for a process that is already dead.
    time.sleep(1.5)
    if process.poll() is not None:
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8")[-1000:]
        except OSError:
            pass
        return {"ok": False, "message": f"Web 后端进程已退出 (returncode={process.returncode})，日志: {log_path}\n{tail}",
                "pid": process.pid, "already_running": False}

    return {"ok": True, "message": f"Web 后端已启动 (pid={process.pid}, 日志: {log_path})",
            "pid": process.pid, "already_running": False}


def stop_web_backend(config: AppConfig) -> dict:
    """SIGTERM the pid recorded in the pidfile, then remove the pidfile."""
    status = web_backend_status(config)
    pid_path = web_pid_path(config)

    if not status["pid"]:
        return {"ok": True, "message": "Web 后端未在运行（无 pidfile）"}

    if not status["running"]:
        pid_path.unlink(missing_ok=True)
        return {"ok": True, "message": f"Web 后端进程 (pid={status['pid']}) 已不存在，清理 pidfile"}

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(status["pid"]), "/T", "/F"],
                            capture_output=True, text=True, timeout=10, check=False)
        else:
            _kill_process_tree(status["pid"])
            # Give it a moment to shut down gracefully before declaring done.
            for _ in range(10):
                if not _pid_is_running(status["pid"]):
                    break
                time.sleep(0.3)
    except (OSError, ProcessLookupError) as exc:
        pid_path.unlink(missing_ok=True)
        return {"ok": True, "message": f"停止 Web 后端时遇到 {exc}（进程可能已退出），已清理 pidfile"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "message": f"停止 Web 后端失败: {exc}"}

    pid_path.unlink(missing_ok=True)
    return {"ok": True, "message": f"Web 后端 (pid={status['pid']}) 已停止"}


# ---------------------------------------------------------------------------
# Frontend (built Vue3 app, served via `vite preview`, pidfile-managed
# exactly like the web backend above)
# ---------------------------------------------------------------------------

def frontend_pid_path(config: AppConfig) -> Path:
    return Path(config.data_dir) / "frontend.pid"


def _frontend_dir() -> Path:
    return _project_root() / "frontend"


def frontend_status(config: AppConfig) -> dict:
    pid_path = frontend_pid_path(config)
    if not pid_path.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return {"running": False, "pid": None}
    if _pid_is_running(pid):
        return {"running": True, "pid": pid}
    return {"running": False, "pid": pid}


def _ensure_frontend_built(config: AppConfig) -> dict:
    """Run `npm install` + `npm run build` if `frontend/dist/index.html`
    doesn't exist yet. Skipped (not re-built) on every start once a build
    is present — `cli clean` does not remove `dist/`, so a rebuild is only
    needed after pulling new frontend source, not on every start/stop cycle.
    """
    frontend_dir = _frontend_dir()
    dist_index = frontend_dir / "dist" / "index.html"
    if dist_index.exists():
        return {"ok": True, "message": "前端已构建，跳过 (frontend/dist 已存在)"}

    if not frontend_dir.exists():
        return {"ok": False, "message": f"未找到前端目录: {frontend_dir}"}

    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    try:
        install = subprocess.run([npm, "install"], cwd=str(frontend_dir),
                                  capture_output=True, text=True, timeout=300, check=False)
        if install.returncode != 0:
            return {"ok": False, "message": f"npm install 失败:\n{install.stderr[-2000:]}"}

        build = subprocess.run([npm, "run", "build"], cwd=str(frontend_dir),
                                capture_output=True, text=True, timeout=300, check=False)
        if build.returncode != 0:
            return {"ok": False, "message": f"npm run build 失败:\n{build.stderr[-2000:]}"}
    except FileNotFoundError:
        return {"ok": False, "message": "未找到 npm 命令，请先在服务器安装 Node.js/npm"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "前端构建超时"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "message": f"前端构建异常: {exc}"}

    return {"ok": True, "message": "前端构建完成 (npm install && npm run build)"}


def start_frontend(config: AppConfig) -> dict:
    """Build the frontend if needed, then serve `dist/` via `vite preview`
    in the background (pidfile-managed, same lifecycle pattern as the web
    backend). Returns {ok, message, pid}."""
    status = frontend_status(config)
    if status["running"]:
        return {"ok": True, "message": f"前端已在运行 (pid={status['pid']})", "pid": status["pid"]}

    build_result = _ensure_frontend_built(config)
    if not build_result["ok"]:
        return {"ok": False, "message": build_result["message"], "pid": None}

    frontend_dir = _frontend_dir()
    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    cmd = [npx, "vite", "preview", "--host", "0.0.0.0", "--port", str(config.frontend_port)]

    log_dir = Path(config.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "frontend.log"

    try:
        log_file = open(log_path, "a", encoding="utf-8")
        popen_kwargs: dict = {
            "cwd": str(frontend_dir),
            "stdout": log_file,
            "stderr": log_file,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(cmd, **popen_kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "message": f"启动前端失败: {exc}", "pid": None}

    frontend_pid_path(config).parent.mkdir(parents=True, exist_ok=True)
    frontend_pid_path(config).write_text(str(process.pid), encoding="utf-8")

    time.sleep(1.5)
    if process.poll() is not None:
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8")[-1000:]
        except OSError:
            pass
        return {"ok": False, "message": f"前端进程已退出 (returncode={process.returncode})，日志: {log_path}\n{tail}",
                "pid": process.pid}

    return {"ok": True, "message": f"前端已启动 (pid={process.pid}, 端口={config.frontend_port}, 日志: {log_path})",
            "pid": process.pid}


def stop_frontend(config: AppConfig) -> dict:
    status = frontend_status(config)
    pid_path = frontend_pid_path(config)

    if not status["pid"]:
        return {"ok": True, "message": "前端未在运行（无 pidfile）"}

    if not status["running"]:
        pid_path.unlink(missing_ok=True)
        return {"ok": True, "message": f"前端进程 (pid={status['pid']}) 已不存在，清理 pidfile"}

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(status["pid"]), "/T", "/F"],
                            capture_output=True, text=True, timeout=10, check=False)
        else:
            _kill_process_tree(status["pid"])
            for _ in range(10):
                if not _pid_is_running(status["pid"]):
                    break
                time.sleep(0.3)
    except (OSError, ProcessLookupError) as exc:
        pid_path.unlink(missing_ok=True)
        return {"ok": True, "message": f"停止前端时遇到 {exc}（进程可能已退出），已清理 pidfile"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "message": f"停止前端失败: {exc}"}

    pid_path.unlink(missing_ok=True)
    return {"ok": True, "message": f"前端 (pid={status['pid']}) 已停止"}


# ---------------------------------------------------------------------------
# Component orchestration: start / stop / clean
# ---------------------------------------------------------------------------

def start_all(config: AppConfig) -> list[dict]:
    """Start SearXNG + every rendered model compose file, THEN the web
    backend (+ frontend).

    Order matters here: `web.main.on_startup()` calls
    `web.state.reconcile_from_disk()` exactly once, at FastAPI startup, to
    rebuild its in-memory "what's loaded" registry from real container
    state. If the web backend were started first, that reconcile would run
    before any model container exists yet — every vllm container slot
    would show as "running but no model attached" in 实时总览/组件状态
    until the next manual reconcile (there isn't one), even though the
    model genuinely is loaded a few seconds later. Docker containers
    starting first removes that race entirely.

    Returns a list of {component, ok, message} dicts in execution order.
    """
    results: list[dict] = []

    searxng_compose = _project_root() / "deploy" / "searxng-compose.yml"
    if searxng_compose.exists():
        searxng_result = SearxngManager(compose_path=searxng_compose).start()
        results.append({
            "component": "searxng", "ok": searxng_result.ok,
            "message": "SearXNG 已启动" if searxng_result.ok
            else (searxng_result.error or searxng_result.stderr or "SearXNG 启动失败"),
        })
    else:
        results.append({"component": "searxng", "ok": True,
                         "message": f"未找到 {searxng_compose}，跳过（尚未部署 SearXNG）"})

    docker_manager = DockerComposeManager(model_root_host=config.model_root_dir,
                                           cuda_compat_dir=config.cuda_compat_dir)

    model_files = model_compose_files(config)
    # Self-heal the (local-only patched) vllm image before bringing up model
    # containers — otherwise `docker compose up` would fail trying to pull a
    # tag that exists on no registry. Only relevant if there are model compose
    # files to start; build is a no-op when the image already exists.
    if model_files and not image_exists(config.vllm_image):
        ensure = ensure_vllm_image(config.vllm_image)
        results.append({"component": "vllm-image", "ok": ensure.ok,
                         "message": (f"已构建 {config.vllm_image}" if ensure.ok
                                     else (ensure.error or ensure.stderr or "vllm 镜像准备失败"))})

    for compose_path in model_files:
        result = docker_manager.start(compose_path)
        results.append({"component": compose_path.stem, "ok": result.ok,
                         "message": "已启动" if result.ok else (result.error or result.stderr or "启动失败")})

    web_result = start_web_backend(config)
    results.append({"component": "web", "ok": web_result["ok"], "message": web_result["message"]})

    frontend_result = start_frontend(config)
    results.append({"component": "frontend", "ok": frontend_result["ok"], "message": frontend_result["message"]})

    return results


def stop_all(config: AppConfig) -> list[dict]:
    """Stop (not remove) every component: web pidfile + `docker compose stop`
    on every model compose file + searxng."""
    results: list[dict] = []

    web_result = stop_web_backend(config)
    results.append({"component": "web", "ok": web_result["ok"], "message": web_result["message"]})

    frontend_result = stop_frontend(config)
    results.append({"component": "frontend", "ok": frontend_result["ok"], "message": frontend_result["message"]})

    docker_manager = DockerComposeManager(model_root_host=config.model_root_dir,
                                           cuda_compat_dir=config.cuda_compat_dir)

    searxng_compose = _project_root() / "deploy" / "searxng-compose.yml"
    if searxng_compose.exists():
        # Note: SearxngManager.stop() in core/docker_helper.py actually maps
        # to `down` (matching the existing searxng_router "stop" semantics).
        # For the CLI's "stop" (non-destructive) we deliberately use plain
        # `docker compose stop` via DockerComposeManager instead, so `cli
        # stop` never removes the SearXNG container/network.
        result = docker_manager.stop(searxng_compose)
        results.append({"component": "searxng", "ok": result.ok,
                         "message": "已停止" if result.ok else (result.error or result.stderr or "停止失败")})
    else:
        results.append({"component": "searxng", "ok": True, "message": "未部署，跳过"})

    for compose_path in model_compose_files(config):
        result = docker_manager.stop(compose_path)
        results.append({"component": compose_path.stem, "ok": result.ok,
                         "message": "已停止" if result.ok else (result.error or result.stderr or "停止失败")})

    return results


def clean_all(config: AppConfig) -> list[dict]:
    """stop_all() + `docker compose down` on every compose file + remove
    web.pid. Does NOT touch model_scan_result.json, perf reports,
    metrics_history.jsonl, or config/settings.yaml — only run-state."""
    results = stop_all(config)

    docker_manager = DockerComposeManager(model_root_host=config.model_root_dir,
                                           cuda_compat_dir=config.cuda_compat_dir)

    searxng_compose = _project_root() / "deploy" / "searxng-compose.yml"
    if searxng_compose.exists():
        result = docker_manager.down(searxng_compose)
        results.append({"component": "searxng (down)", "ok": result.ok,
                         "message": "已清理" if result.ok else (result.error or result.stderr or "清理失败")})

    for compose_path in model_compose_files(config):
        result = docker_manager.down(compose_path)
        results.append({"component": f"{compose_path.stem} (down)", "ok": result.ok,
                         "message": "已清理" if result.ok else (result.error or result.stderr or "清理失败")})

    pid_path = web_pid_path(config)
    if pid_path.exists():
        pid_path.unlink(missing_ok=True)
        results.append({"component": "web.pid", "ok": True, "message": "已删除"})

    frontend_pid = frontend_pid_path(config)
    if frontend_pid.exists():
        frontend_pid.unlink(missing_ok=True)
        results.append({"component": "frontend.pid", "ok": True, "message": "已删除"})

    return results
