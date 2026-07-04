"""Background metrics collection loop.

Runs as an `asyncio.create_task` started from main.py's startup event.
Reads GPU stats via pynvml and CPU/memory stats via psutil; either or both
libraries may be missing/fail on a given host (e.g. local dev machine,
or a container without NVML access) — in that case the corresponding
fields degrade to None rather than raising, so the loop never crashes and
the frontend can render "data unavailable" placeholders.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from web.ws_hub import ConnectionManager

logger = logging.getLogger(__name__)

METRICS_INTERVAL_SECONDS = 10

# Simple append-only JSONL history store. Chosen over SQLite for this phase
# because the access pattern is "append one small dict every 10s, read back
# a bounded recent window" — a single file with line-based append and a
# tail-bounded read satisfies that with zero schema/migration overhead.
# Revisit with SQLite if query patterns get more complex (e.g. arbitrary
# time-range aggregation across many days).
MAX_HISTORY_LINES = 24 * 60 * 6  # ~1 day at a 10s interval, generous cap


def _history_path(data_dir: str) -> Path:
    path = Path(data_dir) / "metrics_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_metrics_history(data_dir: str, snapshot: dict) -> None:
    """Append one timestamped snapshot to the JSONL history file.

    Best-effort: a write failure (disk full, permissions) is logged and
    swallowed rather than crashing the metrics loop — losing one history
    point is far less harmful than killing the live websocket broadcast.
    """
    path = _history_path(data_dir)
    record = {"ts": time.time(), **snapshot}
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("写入 metrics_history.jsonl 失败: %s", exc)
        return

    _maybe_truncate(path)


def _maybe_truncate(path: Path) -> None:
    """Cap the history file to MAX_HISTORY_LINES by rewriting it with only
    the most recent lines. Cheap-and-simple: only runs the rewrite once
    every ~100 appends worth of overshoot to avoid rewriting on every tick."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= MAX_HISTORY_LINES + 100:
        return
    trimmed = lines[-MAX_HISTORY_LINES:]
    try:
        with path.open("w", encoding="utf-8") as fh:
            fh.writelines(trimmed)
    except OSError as exc:
        logger.warning("截断 metrics_history.jsonl 失败: %s", exc)


def read_metrics_history(data_dir: str, window_seconds: float) -> list[dict]:
    """Read history points with ts within the last `window_seconds`.

    Reads the whole file (bounded by MAX_HISTORY_LINES, so at most ~1 day
    of 10s-interval points — a few MB at most) rather than indexing by
    byte offset; simplest correct implementation for this phase's data
    volume. Revisit if window queries need to scale to many days/users.
    """
    path = _history_path(data_dir)
    if not path.exists():
        return []
    cutoff = time.time() - window_seconds
    points: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("ts", 0) >= cutoff:
                    points.append(record)
    except OSError as exc:
        logger.warning("读取 metrics_history.jsonl 失败: %s", exc)
        return []
    return points


def _read_cpu_mem() -> dict:
    """Best-effort CPU/memory read via psutil. Returns Nones on failure."""
    try:
        import psutil  # imported lazily so its absence doesn't break startup

        vm = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "mem_used_gb": round((vm.total - vm.available) / (1024 ** 3), 2),
            "mem_free_gb": round(vm.available / (1024 ** 3), 2),
            "cache_gb": round(getattr(vm, "cached", 0) / (1024 ** 3), 2) if hasattr(vm, "cached") else None,
        }
    except Exception as exc:  # pragma: no cover - defensive, missing lib / unsupported platform
        logger.debug("psutil read failed: %s", exc)
        return {"cpu_percent": None, "mem_used_gb": None, "mem_free_gb": None, "cache_gb": None}


_NVML_STATE = {"initialized": False, "failed": False}


def _read_gpu() -> dict:
    """Best-effort GPU read via pynvml. Returns Nones on failure.

    NVML init is attempted once; if it fails (no driver, no permissions,
    library missing) we remember that and stop retrying every loop tick.
    """
    if _NVML_STATE["failed"]:
        return {"gpu_percent": None, "gpu_temp_c": None, "power_watts": None}

    try:
        import pynvml

        if not _NVML_STATE["initialized"]:
            pynvml.nvmlInit()
            _NVML_STATE["initialized"] = True

        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        power_mw = pynvml.nvmlDeviceGetPowerUsage(handle)
        return {
            "gpu_percent": float(util.gpu),
            "gpu_temp_c": float(temp),
            "power_watts": round(power_mw / 1000.0, 1),
        }
    except Exception as exc:  # pragma: no cover - defensive, missing lib / no GPU / no permission
        logger.warning("pynvml read failed, disabling further GPU polling: %s", exc)
        _NVML_STATE["failed"] = True
        return {"gpu_percent": None, "gpu_temp_c": None, "power_watts": None}


def collect_metrics_snapshot() -> dict:
    """Build one metrics payload combining CPU/mem and GPU readings."""
    snapshot = {}
    snapshot.update(_read_cpu_mem())
    snapshot.update(_read_gpu())
    return snapshot


async def _fetch_logs(component: str, lines: int) -> str:
    """Async wrapper used by the WS log streamer — resolves a component's
    logs off the event loop (docker logs / file read can block)."""
    from web.routers.logs_router import resolve_logs  # lazy: avoid import cycle

    content, _found = await asyncio.to_thread(resolve_logs, component, lines)
    return content or ""


# ---------------------------------------------------------------------------
# Per-model tok/s sampling — the SINGLE source that differences vLLM's
# cumulative token counters, so the value is consistent everywhere:
#   - persisted into metrics history (model_tps) for the 运行观测 trend charts,
#   - read by /api/models/{name}/runtime-stats (which must NOT difference again,
#     or both callers would see only partial deltas).
# Keyed by model name. Sampled every collector tick (like system metrics), so
# the tok/s trend has the same ~1-day persisted history as CPU/GPU.
# ---------------------------------------------------------------------------
LATEST_TPS: dict[str, dict] = {}
_TPS_LAST: dict[str, tuple[float, float, float]] = {}  # name -> (monotonic_ts, prompt_total, gen_total)


def _sample_model_tps_sync() -> dict:
    import httpx
    from web.routers.runtime_stats_router import _parse_prometheus_text
    from web.state import registry

    result: dict[str, dict] = {}
    for name in registry.all_loaded_names():
        loaded = registry.get(name)
        if not loaded or not loaded.host_port:
            continue
        try:
            resp = httpx.get(f"http://127.0.0.1:{loaded.host_port}/metrics", timeout=3.0)
            if resp.status_code != 200:
                continue
            raw = _parse_prometheus_text(resp.text)
        except Exception:  # pragma: no cover - model still starting / unreachable
            continue

        prompt_total = sum(v for k, v in raw.items() if k == "vllm:prompt_tokens_total")
        gen_total = sum(v for k, v in raw.items() if k == "vllm:generation_tokens_total")
        now = time.monotonic()
        prefill = decode = None
        prev = _TPS_LAST.get(name)
        if prev is not None and now > prev[0]:
            dt = now - prev[0]
            d_prompt, d_gen = prompt_total - prev[1], gen_total - prev[2]
            prefill = round(d_prompt / dt, 1) if d_prompt >= 0 else None
            decode = round(d_gen / dt, 1) if d_gen >= 0 else None
        _TPS_LAST[name] = (now, prompt_total, gen_total)
        entry = {
            "prefill_tps": prefill, "decode_tps": decode,
            "prompt_tokens_total": prompt_total, "generation_tokens_total": gen_total,
        }
        result[name] = entry
        LATEST_TPS[name] = entry
    # Drop models no longer loaded so stale entries don't linger.
    for stale in set(LATEST_TPS) - set(result):
        LATEST_TPS.pop(stale, None)
        _TPS_LAST.pop(stale, None)
    return result


class MetricsCollector:
    """Owns the periodic asyncio loop that broadcasts `metrics` over WS."""

    def __init__(self, ws_manager: ConnectionManager, interval: int = METRICS_INTERVAL_SECONDS,
                 data_dir: str | None = None) -> None:
        self._ws_manager = ws_manager
        self._interval = interval
        self._data_dir = data_dir
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stopped.clear()
            self._task = asyncio.create_task(self._run(), name="metrics-collector")
            logger.info("MetricsCollector started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            # GPU/CPU snapshot is cheap (pynvml NVML calls + psutil, no
            # subprocess) and feeds the 1-day history chart, so collect &
            # persist it every tick regardless of who's watching.
            try:
                snapshot = await asyncio.to_thread(collect_metrics_snapshot)
                # Sample per-model tok/s (single differencing source) and
                # persist it alongside the system snapshot so the 运行观测
                # tok/s charts get the same persisted ~1-day history as CPU/GPU.
                tps = await asyncio.to_thread(_sample_model_tps_sync)
                if self._data_dir:
                    record = {**snapshot, "model_tps": tps} if tps else snapshot
                    await asyncio.to_thread(append_metrics_history, self._data_dir, record)
            except Exception as exc:  # pragma: no cover - never let the loop die
                logger.error("MetricsCollector loop iteration failed: %s", exc)
                snapshot = None

            # Broadcasts only matter if someone is actually connected. The
            # page snapshots in particular are *expensive* to build (docker
            # stats, per-model httpx /metrics scrape) — skip ALL of that work
            # when there are zero WS clients, so an idle server with the page
            # closed costs essentially nothing beyond the cheap history sample
            # above. They resume the moment a client connects.
            if self._ws_manager.connection_count > 0:
                if snapshot is not None:
                    try:
                        await self._ws_manager.broadcast("metrics", snapshot)
                    except Exception as exc:  # pragma: no cover
                        logger.error("metrics 广播失败: %s", exc)
                await self._broadcast_page_snapshots()
                # Per-connection live log push (replaces the old REST log
                # polling in the 组件日志 / 模型配置 pages). Only connections
                # that set a log target via the WS "set_log_target" action
                # receive anything.
                await self._ws_manager.push_log_updates(_fetch_logs)

            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def _broadcast_page_snapshots(self) -> None:
        # Lazy imports: web.routers.* import background_tasks (for
        # collect_metrics_snapshot), so importing them at module load here
        # would be circular. Importing inside the method breaks that cycle.
        try:
            from web.routers.overview_router import get_overview
            overview = await asyncio.to_thread(lambda: get_overview().model_dump())
            await self._ws_manager.broadcast("overview", overview)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("overview 快照广播失败: %s", exc)

        try:
            from web.routers.api_directory_router import get_api_directory
            api_dir = await asyncio.to_thread(get_api_directory)
            await self._ws_manager.broadcast("api_directory", api_dir)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("api_directory 快照广播失败: %s", exc)

        try:
            from web.routers.runtime_stats_router import get_runtime_stats
            from web.state import registry

            names = registry.all_loaded_names()
            stats = await asyncio.to_thread(
                lambda: {n: get_runtime_stats(n) for n in names}
            )
            await self._ws_manager.broadcast("runtime_stats", stats)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("runtime_stats 快照广播失败: %s", exc)
