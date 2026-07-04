"""GET /api/metrics/history — query the JSONL-backed metrics history
written by web.background_tasks.MetricsCollector every METRICS_INTERVAL_SECONDS.

Window strings are parsed as `<number><unit>` where unit is one of
`h` (hours) or `d` (days); defaults to `1d` per Project_Task.md's
"运行观测...近1天数据的趋势图表" requirement. Each point also carries
`model_tps` — per-model prefill/decode tok/s sampled into the same metrics
loop (web.background_tasks._sample_model_tps_sync) — so the 运行观测 page's
model-level tok/s charts share the exact same persisted history/window as
the system-level CPU/GPU charts.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status

from core.config import load_config
from web.auth import get_current_user
from web.background_tasks import read_metrics_history
from web.schemas import MetricsHistoryPoint, MetricsHistoryResponse

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(get_current_user)])

_WINDOW_RE = re.compile(r"^(\d+)([hd])$")


def _window_to_seconds(window: str) -> float:
    match = _WINDOW_RE.match(window.strip().lower())
    if not match:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail="window 参数格式应为 <数字>h 或 <数字>d，例如 1d / 12h")
    value, unit = match.groups()
    seconds = int(value) * (3600 if unit == "h" else 86400)
    return float(seconds)


@router.get("/history", response_model=MetricsHistoryResponse)
def get_metrics_history(window: str = "1d") -> MetricsHistoryResponse:
    config = load_config()
    window_seconds = _window_to_seconds(window)
    raw_points = read_metrics_history(config.data_dir, window_seconds)
    points = [
        MetricsHistoryPoint(
            ts=p.get("ts", 0.0),
            cpu_percent=p.get("cpu_percent"),
            mem_used_gb=p.get("mem_used_gb"),
            mem_free_gb=p.get("mem_free_gb"),
            cache_gb=p.get("cache_gb"),
            gpu_percent=p.get("gpu_percent"),
            gpu_temp_c=p.get("gpu_temp_c"),
            power_watts=p.get("power_watts"),
            model_tps=p.get("model_tps"),
        )
        for p in raw_points
    ]
    return MetricsHistoryResponse(window=window, points=points)
