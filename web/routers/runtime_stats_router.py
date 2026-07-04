"""GET /api/models/{name}/runtime-stats — real launch params + vllm metrics.

Three data sources, each independently best-effort (never raises, always
degrades to a friendly "未加载"/None shape rather than a 500):

1. Launch params: read straight from the already-rendered
   `data/compose/{name}.compose.yml` file's `command:` list — that file is
   the actual source of truth for "what flags did we launch this container
   with", so this endpoint does not reconstruct/guess flags from
   param_advisor.
2. KV cache / running+waiting request counts: scraped from the vllm
   container's Prometheus-text `/metrics` endpoint at
   `http://127.0.0.1:{host_port}/metrics`. Metric *names* below
   (`vllm:num_requests_running`, `vllm:num_requests_waiting`,
   `vllm:gpu_cache_usage_perc`) are vllm's documented/typical Prometheus
   metric names as of the v0.x/v1 OpenAI-compatible server. Confirmed these
   three names still exist in the vllm build inside
   nvcr.io/nvidia/vllm:26.06-py3 (vllm 0.22.1) by grepping the container's
   vllm source; the exact set still depends on the build, so parsing stays
   tolerant (`curl http://127.0.0.1:<port>/metrics` after loading a model is
   the definitive check) — see 研发方案.md 阶段七. Parsing is intentionally
   a tolerant prefix/substring match over the metric name (not an exact
   match against one hardcoded name), so close naming variants across vllm
   versions are more likely to still be picked up; metric lines that aren't
   found simply stay None instead of raising or faking a value.
3. Memory usage: reuses the same batched `docker stats` helper that
   components_router.py already added (no duplicate per-call docker stats
   invocation).
"""
from __future__ import annotations

import logging
import re

import httpx
import yaml
from fastapi import APIRouter, Depends

from core.config import load_config
from web.auth import get_current_user
from web.routers.components_router import _docker_stats_mem_by_container
from web.state import compose_dir, registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"], dependencies=[Depends(get_current_user)])

METRICS_TIMEOUT_SECONDS = 3.0

# Tolerant substring match: vllm Prometheus metric names have historically
# been prefixed "vllm:"; matching by substring (rather than an exact key)
# means small version-to-version naming drift is less likely to silently
# break this. Confirm the exact real names on the ARM64 box and tighten
# these if needed (see module docstring).
METRIC_NAME_HINTS = {
    "num_requests_running": "num_requests_running",
    "num_requests_waiting": "num_requests_waiting",
    # Confirmed on the real ARM64 box (nvcr.io/nvidia/vllm:26.06-py3,
    # vLLM 0.22.1; also held on 26.05.post1/0.21): the real metric is
    # `vllm:kv_cache_usage_perc`, not `gpu_cache_usage_perc` as first guessed.
    "gpu_cache_usage_perc": "kv_cache_usage_perc",
}

# tok/s (prefill/decode) is computed by the single sampler in
# web.background_tasks (_sample_model_tps_sync) and read here from LATEST_TPS
# — see get_runtime_stats. Differencing the counters in two places would
# split the deltas and corrupt both readings.


def _read_compose_command(compose_path: str) -> list[str] | None:
    """Read the `command:` list out of a rendered compose file as-is."""
    try:
        with open(compose_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        services = data.get("services") or {}
        for service in services.values():
            command = service.get("command")
            if isinstance(command, list):
                return [str(c) for c in command]
        return None
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("读取 compose 启动参数失败: %s", exc)
        return None


def _parse_prometheus_text(text: str) -> dict[str, float]:
    """Tolerant line-based parse of Prometheus text exposition format.

    For each line like `vllm:num_requests_running{model_name="x"} 3.0`,
    extract the metric name (before `{` or first whitespace) and the
    trailing numeric value. No prometheus_client dependency needed — the
    format is simple enough for plain string handling, and a malformed/
    unexpected line is just skipped rather than aborting the whole parse.
    """
    values: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([a-zA-Z0-9_:]+)(\{[^}]*\})?\s+([0-9.eE+\-]+)\s*$", line)
        if not match:
            continue
        metric_name, _labels, value_str = match.groups()
        try:
            values[metric_name] = float(value_str)
        except ValueError:
            continue
    return values


def _extract_vllm_metrics(raw_metrics: dict[str, float]) -> dict[str, float | None]:
    result: dict[str, float | None] = {key: None for key in METRIC_NAME_HINTS}
    for metric_name, value in raw_metrics.items():
        for result_key, hint in METRIC_NAME_HINTS.items():
            if hint in metric_name:
                result[result_key] = value
    return result


@router.get("/{name}/runtime-stats")
def get_runtime_stats(name: str) -> dict:
    loaded = registry.get(name)
    if loaded is None:
        return {
            "model_name": name,
            "loaded": False,
            "message": "当前模型未加载",
            "launch_command": None,
            "memory_usage_mb": None,
            "metrics": None,
        }

    launch_command = _read_compose_command(loaded.compose_path)

    mem_by_container = _docker_stats_mem_by_container()
    memory_usage_mb = mem_by_container.get(loaded.container_name)

    metrics_payload: dict | None = None
    metrics_message: str | None = None

    if loaded.host_port:
        metrics_url = f"http://127.0.0.1:{loaded.host_port}/metrics"
        try:
            response = httpx.get(metrics_url, timeout=METRICS_TIMEOUT_SECONDS)
            if response.status_code == 200:
                raw_metrics = _parse_prometheus_text(response.text)
                extracted = _extract_vllm_metrics(raw_metrics)
                # tok/s comes from the single sampler in background_tasks
                # (LATEST_TPS) — differencing the counters here too would make
                # both callers see only partial deltas. Gauges below are read
                # from this scrape directly (safe; not differenced).
                from web.background_tasks import LATEST_TPS
                tok = LATEST_TPS.get(name, {})
                metrics_payload = {
                    "num_requests_running": extracted["num_requests_running"],
                    "num_requests_waiting": extracted["num_requests_waiting"],
                    "gpu_cache_usage_perc": extracted["gpu_cache_usage_perc"],
                    "prefill_tps": tok.get("prefill_tps"),
                    "decode_tps": tok.get("decode_tps"),
                    "prompt_tokens_total": tok.get("prompt_tokens_total"),
                    "generation_tokens_total": tok.get("generation_tokens_total"),
                }
            else:
                metrics_message = f"/metrics 返回非 200 状态码: {response.status_code}"
        except httpx.HTTPError as exc:
            metrics_message = f"无法连接 {metrics_url}（模型可能仍在启动中）: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            metrics_message = f"解析 /metrics 失败: {exc}"
    else:
        metrics_message = "未知端口，无法采集 /metrics"

    return {
        "model_name": name,
        "loaded": True,
        "engine": loaded.engine,
        "container_name": loaded.container_name,
        "host_port": loaded.host_port,
        "launch_command": launch_command,
        "memory_usage_mb": memory_usage_mb,
        "metrics": metrics_payload,
        "metrics_message": metrics_message,
    }
