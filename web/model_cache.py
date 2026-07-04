"""Process-wide cache of the local model scan result.

Why this exists: scanning the model root is *expensive* (it stat-walks every
file, parses each config.json, parses multi-MB safetensors index.json for
completeness checks, reads GGUF tensor headers). Doing that on every
/api/overview, /api/models, /api/api-directory request — and on every 10s
WebSocket push — wastes a lot of IO/CPU. So scanning is now an explicit,
manual action; everything else reads this cache.

Source of truth is the on-disk `data/model_scan_result.json`, written by
either `cli model_check` or the Web `POST /api/models/rescan` action. This
cache:
  - loads from disk lazily on first use,
  - reloads automatically when the file's mtime changes (so a scan triggered
    by the CLI in a separate process is picked up by the running web backend
    without a restart),
  - bootstraps with a single scan only if no result file exists yet (first
    run), so a fresh deployment isn't empty — never re-scans automatically
    after that.

Thread-safe: FastAPI runs sync route handlers in a threadpool, so concurrent
reads are possible.
"""
from __future__ import annotations

import threading

from core.config import AppConfig
from core.model_scanner import (
    ModelInfo,
    load_scan_result,
    save_scan_result,
    scan_models,
    scan_result_path,
)

_lock = threading.Lock()
_cache: list[ModelInfo] | None = None
_meta: dict = {"scanned_at": None, "model_root_dir": None}
_loaded_mtime: float | None = None


def _disk_mtime(config: AppConfig) -> float | None:
    try:
        return scan_result_path(config.data_dir).stat().st_mtime
    except OSError:
        return None


def get_models(config: AppConfig) -> list[ModelInfo]:
    """Return the cached model list, loading from disk / bootstrapping a
    one-time scan as described in the module docstring. Never triggers an
    automatic re-scan once a result exists."""
    global _cache, _meta, _loaded_mtime
    with _lock:
        mtime = _disk_mtime(config)
        if _cache is not None and mtime == _loaded_mtime:
            return _cache

        if mtime is not None:
            loaded = load_scan_result(config.data_dir)
            if loaded is not None:
                _cache = loaded["models"]
                _meta = {"scanned_at": loaded["scanned_at"], "model_root_dir": loaded["model_root_dir"]}
                _loaded_mtime = mtime
                return _cache

        # No result file yet → first-run bootstrap: one scan, then persist.
        models = scan_models(config.model_root_dir)
        payload = save_scan_result(models, config.data_dir, config.model_root_dir)
        _cache = models
        _meta = {"scanned_at": payload["scanned_at"], "model_root_dir": config.model_root_dir}
        _loaded_mtime = _disk_mtime(config)
        return _cache


def rescan(config: AppConfig) -> dict:
    """Explicit manual rescan: walk the model root, persist, refresh cache.
    Returns a small summary for the API/CLI to report."""
    global _cache, _meta, _loaded_mtime
    models = scan_models(config.model_root_dir)
    with _lock:
        payload = save_scan_result(models, config.data_dir, config.model_root_dir)
        _cache = models
        _meta = {"scanned_at": payload["scanned_at"], "model_root_dir": config.model_root_dir}
        _loaded_mtime = _disk_mtime(config)
    return {
        "total": len(models),
        "invalid": len([m for m in models if not m.valid]),
        "general": len([m for m in models if not m.is_embedding]),
        "embedding": len([m for m in models if m.is_embedding]),
        "scanned_at": payload["scanned_at"],
    }


def get_meta(config: AppConfig) -> dict:
    """Metadata about the current cached result (loads it if needed)."""
    get_models(config)
    with _lock:
        return dict(_meta)
