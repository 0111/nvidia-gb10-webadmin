"""Model listing, parameter recommendation, load/unload.

All scanning/recommendation logic is delegated to core.model_scanner and
core.param_advisor — this router only adapts results into Pydantic
schemas and drives core.docker_helper.DockerComposeManager for the actual
load/unload side effects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from core import param_advisor
from core.config import load_config
from core.docker_helper import DockerComposeManager, container_logs, ensure_vllm_image, image_exists
from core.model_scanner import ModelInfo, list_embedding_models, list_general_models
from web import model_cache
from web.auth import get_current_user
from web.background_tasks import collect_metrics_snapshot
from web.schemas import (
    GpuMemoryHintOut,
    ModelInfoOut,
    ModelListResponse,
    ModelLoadRequest,
    ModelLoadResponse,
    ModelUnloadResponse,
    ParamEntryOut,
    ParamsResponse,
)
from web.state import compose_dir, current_model_in_container, real_load_status, registry
from web.ws_hub import manager as ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"], dependencies=[Depends(get_current_user)])


async def _detect_early_load_failure(container_name: str, checks: int = 3, interval: float = 2.0) -> str | None:
    """Catch a vllm container that crashes within seconds of starting.

    `docker compose up -d` returns as soon as the container is *created*, not
    when vllm has actually loaded the model — and vllm's normal weight load
    keeps the container in "running" for minutes. So a container that reaches
    "exited"/"dead" within this short window is an unambiguous load failure
    (bad params, missing/incompatible weights, OOM-at-init, vllm error-exit),
    not slow loading. Returns a short human-readable error summary (with the
    telling log line) on failure, or None if the container is still alive
    after the window (still loading — the components view keeps watching the
    real state from there on).
    """
    for _ in range(checks):
        await asyncio.sleep(interval)
        try:
            r = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}\t{{.State.ExitCode}}", container_name],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except Exception:  # pragma: no cover - defensive
            return None
        out = (r.stdout or "").strip()
        if r.returncode != 0 or "\t" not in out:
            continue
        state, _, code = out.partition("\t")
        if state.strip() in ("exited", "dead"):
            logs = container_logs(container_name, tail=50)
            tail = (logs.stdout or "") if logs.ok else ""
            err_lines = [
                ln.strip() for ln in tail.splitlines()
                if any(k in ln for k in ("Error", "error", "Traceback", "raise ", "Exception", "CUDA", "ValueError", "RuntimeError"))
            ]
            summary = err_lines[-1] if err_lines else f"容器启动后立即退出(exit code {code.strip()})"
            return f"容器加载失败({state.strip()}): {summary[:240]}"
    return None


@router.get("/tool-call-parsers")
def list_tool_call_parsers() -> dict:
    """Static reference list for the API调试(工具调试) page: every
    --tool-call-parser value the current vllm image supports, read from
    `vllm serve --help=Frontend` on the real server. Lets a user filling in
    a `tools` JSON body for a model know what parser names are actually
    valid, without guessing or reading server logs."""
    return {"tool_call_parsers": param_advisor.TOOL_CALL_PARSER_OPTIONS}


def _find_model(name: str) -> ModelInfo:
    config = load_config()
    models = model_cache.get_models(config)
    for m in models:
        if m.name == name:
            return m
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到模型: {name}")


@router.get("", response_model=ModelListResponse)
def list_models(type: Literal["general", "embedding"] | None = None) -> ModelListResponse:
    config = load_config()
    models = model_cache.get_models(config)

    if type == "general":
        models = list_general_models(models)
    elif type == "embedding":
        models = list_embedding_models(models)

    # Resolve load status from REAL container state, not just the registry:
    # a model whose container crashed after load still lingers in the registry
    # but must show as "error" (加载失败), not "running" — otherwise the page
    # keeps offering 已加载/卸载 for a dead model. See state.real_load_status.
    status_map = real_load_status()

    def _load_status(m) -> str:
        status = status_map.get(m.name)
        if status == "running":
            return "running"
        if status == "failed":
            return "error"
        return "unloaded"

    out = [ModelInfoOut.from_core(m, load_status=_load_status(m)) for m in models]
    meta = model_cache.get_meta(config)
    return ModelListResponse(
        model_root_dir=config.model_root_dir, total=len(out), models=out,
        scanned_at=meta.get("scanned_at"),
    )


@router.post("/rescan")
def rescan_models() -> dict:
    """Manually trigger a fresh scan of the model root (the expensive,
    file-reading operation) and persist the result. This is the ONLY thing
    that scans on demand — every read path uses the cached result instead.
    """
    config = load_config()
    summary = model_cache.rescan(config)
    return {"ok": True, "message": "模型扫描完成", **summary}


@router.get("/{name}/audit")
def audit_model(name: str) -> dict:
    """Consolidated completeness/readiness audit for one model: every object
    worth reviewing before loading — config/tokenizer/shard 完整性、架构/类型/
    量化/上下文、vLLM 是否支持、推荐启动参数、静态校验结论与错误摘要。
    Returns both a flat `fields` map and an ordered `rows` list (key/label/
    value) for direct table rendering. See core.model_audit.build_audit."""
    from core.model_audit import build_audit
    return build_audit(_find_model(name))


_GPU_MEM_RESERVE_GB = 4.0  # CUDA context/driver overhead psutil doesn't see


def _gpu_memory_hint() -> GpuMemoryHintOut | None:
    """Advisory max for gpu_memory_utilization from current free memory.

    On GB10 the unified pool is shared, so a co-resident model reduces how
    high gpu_memory_utilization can safely go. vllm checks CUDA free memory at
    startup (gpu_memory_utilization × total must fit), but pynvml GPU-memory
    isn't supported on GB10, so we estimate from psutil — which reads a few GiB
    MORE free than CUDA actually sees (context/driver overhead). We therefore
    reserve a buffer and floor, so following the hint reliably loads rather
    than failing by a hair. Non-binding — purely a reminder."""
    import math

    snap = collect_metrics_snapshot()
    free = snap.get("mem_free_gb")
    used = snap.get("mem_used_gb")
    if free is None or used is None:
        return None
    total = round(free + used, 2)
    suggested = None
    if total > 0:
        usable = max(0.0, free - _GPU_MEM_RESERVE_GB)
        suggested = max(0.0, min(0.95, math.floor((usable / total) * 100) / 100))
    return GpuMemoryHintOut(mem_free_gb=free, mem_total_gb=total, suggested_max=suggested)


@router.get("/{name}/params", response_model=ParamsResponse)
def get_model_params(name: str) -> ParamsResponse:
    model = _find_model(name)
    raw_params = param_advisor.recommend_vllm_params(model)
    params = {k: ParamEntryOut(**v) for k, v in raw_params.items()}
    return ParamsResponse(model_name=name, engine="vllm", params=params,
                          gpu_memory_hint=_gpu_memory_hint())


@router.post("/{name}/load", response_model=ModelLoadResponse)
async def load_model(name: str, payload: ModelLoadRequest) -> ModelLoadResponse:
    model = _find_model(name)
    config = load_config()

    # Integrity gate: a model that failed file validation (e.g. a missing
    # safetensors shard) cannot possibly load — refuse up front with the
    # concrete reason rather than letting vllm crash mid-startup with an
    # opaque error.
    if not model.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"模型 {name} 文件校验未通过，无法加载：{'；'.join(model.validation_errors)}",
        )

    # Single-model-per-type rule: 通用大模型 and 嵌入式大模型 each have exactly
    # one fixed container slot (gb10-vllm-general / gb10-vllm-embedding), so
    # only one model of each kind can run at a time. If that slot is already
    # serving a *different* model, refuse the load and tell the user to
    # unload the current one first — rather than silently clobbering it (the
    # previous behavior, which tore down the running model's compose file and
    # replaced it without warning). Reloading the *same* model name is still
    # allowed (idempotent restart). Truth is read from the live container,
    # not the in-memory registry, so a CLI-started or pre-existing model is
    # also respected.
    target_container = "gb10-vllm-embedding" if model.is_embedding else "gb10-vllm-general"
    current_model = current_model_in_container(target_container)
    if current_model is not None and current_model != name:
        model_type_label = "嵌入式大模型" if model.is_embedding else "通用大模型"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"当前{model_type_label}已加载「{current_model}」，"
                f"每类模型只能同时加载1个。请先卸载「{current_model}」后再加载「{name}」。"
            ),
        )

    if model.format == "gguf":
        if model.gguf_multi_shard:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"模型 {name} 是多分片GGUF，请先用 tools/gguf_merge_shards.py 合并为单文件后再加载",
            )
        if model.gguf_vllm_compatible is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"模型 {name} 的GGUF量化类型不被当前vllm版本的GGUF加载器支持，无法加载",
            )

    # Normalize the flat {name: value} request body back into the
    # {name: {"default": value, ...}} shape DockerComposeManager.render_compose expects.
    vllm_params = {k: {"default": v} for k, v in payload.params.items()}

    manager_helper = DockerComposeManager(
        model_root_host=config.model_root_dir,
        cuda_compat_dir=config.cuda_compat_dir,
    )
    # General and embedding models must not share a default port: loading
    # one of each at the same time previously both defaulted to 8001 and
    # the second container would fail to bind.
    host_port = payload.host_port or (8002 if model.is_embedding else 8001)
    compose_content = manager_helper.render_compose(
        model_info=model,
        vllm_params=vllm_params,
        image=config.vllm_image,
        host_port=host_port,
        api_key=config.vllm_api_key,
    )

    container_name = "gb10-vllm-embedding" if model.is_embedding else "gb10-vllm-general"

    out_dir = compose_dir(config.data_dir)
    # gb10-vllm-general/embedding are fixed, shared container names — only
    # one model of each type can ever run at once. Remove any other
    # compose file that previously targeted the same container so stale
    # files don't accumulate and confuse later state reconciliation
    # (web.state.reconcile_from_disk reads the *running* container's real
    # args, but a pile of unrelated leftover files on disk is still
    # confusing clutter and was the proximate cause of a real bug here).
    for stale in out_dir.glob("*.compose.yml"):
        if stale.name == f"{name}.compose.yml":
            continue
        if f"container_name: {container_name}" in stale.read_text(encoding="utf-8"):
            stale.unlink(missing_ok=True)

    compose_path = out_dir / f"{name}.compose.yml"
    compose_path.write_text(compose_content, encoding="utf-8")

    # Ensure the vllm image exists locally first. The default image is a
    # local-only patched tag (gb10-vllm:26.06-py3-patched); if it's missing,
    # `docker compose up` would try to PULL it and fail opaquely. Auto-build it
    # (idempotent, mirrors bootstrap.sh [5/5]); if it can't be ensured, refuse
    # the load with a clear, actionable message rather than a cryptic pull error.
    if not image_exists(config.vllm_image):
        await ws_manager.broadcast("load_progress", {
            "model_name": name, "stage": "starting",
            "message": f"镜像 {config.vllm_image} 不存在，正在本地构建补丁镜像（首次可能需拉取基础镜像，数分钟）...",
        })
        ensure = ensure_vllm_image(config.vllm_image)
        if not ensure.ok:
            await ws_manager.broadcast("load_progress", {
                "model_name": name, "stage": "error",
                "message": ensure.error or ensure.stderr or "vllm 镜像准备失败",
            })
            return ModelLoadResponse(
                model_name=name, accepted=False,
                message=ensure.error or ensure.stderr or "vllm 镜像准备失败",
                compose_path=str(compose_path),
                command_result={
                    "ok": False, "command": ensure.command, "stdout": ensure.stdout,
                    "stderr": ensure.stderr, "returncode": ensure.returncode, "error": ensure.error,
                },
            )

    await ws_manager.broadcast("load_progress", {
        "model_name": name, "stage": "starting", "message": "正在启动容器...",
    })

    result = manager_helper.start(compose_path)

    if not result.ok:
        await ws_manager.broadcast("load_progress", {
            "model_name": name, "stage": "error",
            "message": result.error or result.stderr or "启动失败",
        })
        return ModelLoadResponse(
            model_name=name, accepted=False,
            message=result.error or result.stderr or "启动失败",
            compose_path=str(compose_path),
            command_result={
                "ok": result.ok, "command": result.command, "stdout": result.stdout,
                "stderr": result.stderr, "returncode": result.returncode, "error": result.error,
            },
        )

    # Container was created/started; verify it doesn't immediately crash
    # before reporting success (otherwise the UI would show a dead model as
    # loaded — the exact failure mode this guards against).
    early_failure = await _detect_early_load_failure(container_name)
    if early_failure is not None:
        # Do NOT register it as loaded — the registry must not advertise a
        # crashed container as a running model.
        registry.mark_unloaded(name)
        await ws_manager.broadcast("load_progress", {
            "model_name": name, "stage": "error", "message": early_failure,
        })
        return ModelLoadResponse(
            model_name=name, accepted=False, message=f"模型加载失败：{early_failure}",
            compose_path=str(compose_path),
            command_result={
                "ok": False, "command": result.command, "stdout": result.stdout,
                "stderr": result.stderr, "returncode": result.returncode, "error": early_failure,
            },
        )

    registry.mark_loading(name, "vllm", str(compose_path), container_name, host_port)
    await ws_manager.broadcast("load_progress", {
        "model_name": name, "stage": "running",
        "message": "容器已启动，模型正在加载（首次加载需数分钟，请在组件状态/运行观测页观察是否就绪）",
    })

    return ModelLoadResponse(
        model_name=name, accepted=True, message="模型加载已启动",
        compose_path=str(compose_path),
        command_result={
            "ok": True, "command": result.command, "stdout": result.stdout,
            "stderr": result.stderr, "returncode": result.returncode, "error": result.error,
        },
    )


@router.post("/{name}/unload", response_model=ModelUnloadResponse)
async def unload_model(name: str) -> ModelUnloadResponse:
    loaded = registry.get(name)
    if loaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail=f"模型 {name} 当前未由本系统加载，无法卸载")

    config = load_config()

    manager_helper = DockerComposeManager(
        model_root_host=config.model_root_dir,
        cuda_compat_dir=config.cuda_compat_dir,
    )
    result = manager_helper.down(loaded.compose_path)

    if result.ok:
        registry.mark_unloaded(name)

    await ws_manager.broadcast("load_progress", {
        "model_name": name,
        "stage": "unloaded" if result.ok else "error",
        "message": "模型已卸载" if result.ok else (result.error or result.stderr or "卸载失败"),
    })

    return ModelUnloadResponse(
        model_name=name,
        accepted=result.ok,
        message="模型已卸载" if result.ok else (result.error or result.stderr or "卸载失败"),
        command_result={
            "ok": result.ok, "command": result.command, "stdout": result.stdout,
            "stderr": result.stderr, "returncode": result.returncode, "error": result.error,
        },
    )
