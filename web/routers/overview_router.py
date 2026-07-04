"""GET /api/overview — one combined payload for the 实时总览 page.

Bundles model load status, env checklist, system resources, and component
status into a single response so the frontend can render the whole page
from one REST call on initial load; live updates afterwards arrive over
the `metrics`/`load_progress` WebSocket topics instead of repeated polling.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core import env_doctor
from core.config import load_config
from core.model_scanner import list_embedding_models, list_general_models
from web import model_cache
from web.auth import get_current_user
from web.background_tasks import collect_metrics_snapshot
from web.routers.components_router import list_components
from web.schemas import (
    CheckResultOut,
    EnvReportOut,
    ModelLoadSummaryOut,
    OverviewResponse,
    SystemResourcesOut,
)
from web.state import real_load_status, registry

router = APIRouter(prefix="/api/overview", tags=["overview"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=OverviewResponse)
def get_overview() -> OverviewResponse:
    config = load_config()

    models = model_cache.get_models(config)
    general_names = {m.name for m in list_general_models(models)}
    embedding_names = {m.name for m in list_embedding_models(models)}
    # Split registry-loaded models into genuinely-running vs crashed, using
    # real container state — a model whose container exited after load must
    # show as 加载失败, not 已加载/卸载. See state.real_load_status.
    status_map = real_load_status()
    running = {n for n, s in status_map.items() if s == "running"}
    failed = {n for n, s in status_map.items() if s == "failed"}

    model_load = ModelLoadSummaryOut(
        general_models_loaded=sorted(running & general_names),
        embedding_models_loaded=sorted(running & embedding_names),
        general_models_failed=sorted(failed & general_names),
        embedding_models_failed=sorted(failed & embedding_names),
    )

    env_report = env_doctor.run_all_checks()
    env_out = EnvReportOut(
        overall_status=env_report.overall_status,
        checks=[CheckResultOut.from_core(c) for c in env_report.checks],
    )

    raw_resources = collect_metrics_snapshot()
    resources = SystemResourcesOut(
        cpu_percent=raw_resources.get("cpu_percent"),
        gpu_percent=raw_resources.get("gpu_percent"),
        power_watts=raw_resources.get("power_watts"),
        mem_used_gb=raw_resources.get("mem_used_gb"),
        mem_free_gb=raw_resources.get("mem_free_gb"),
        cache_gb=raw_resources.get("cache_gb"),
    )

    components = list_components().components

    return OverviewResponse(model_load=model_load, env=env_out, resources=resources, components=components)
