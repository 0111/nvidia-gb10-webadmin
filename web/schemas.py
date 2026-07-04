"""Pydantic request/response models for the Web API.

Field names are snake_case to match the underlying `core` dataclasses and
to be friendly for the Vue3 frontend. These models are pure DTOs: they
never contain business logic, only `from_*` constructors that adapt a
`core` dataclass instance into a serializable shape.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["ok", "warning", "error"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int


# ---------------------------------------------------------------------------
# Env doctor
# ---------------------------------------------------------------------------

class CheckResultOut(BaseModel):
    name: str
    status: Status
    message: str
    suggested_command: str | None = None
    fixable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_core(cls, check: Any) -> "CheckResultOut":
        return cls(
            name=check.name,
            status=check.status,
            message=check.message,
            suggested_command=check.suggested_command,
            fixable=check.fixable,
            details=check.details,
        )


class EnvReportOut(BaseModel):
    overall_status: Status
    checks: list[CheckResultOut]


class FixRequest(BaseModel):
    confirmed: bool = False
    # Only required for the ethernet_speed check; ignored otherwise.
    interface: str | None = None


class FixResultOut(BaseModel):
    name: str
    executed: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    message: str = ""

    @classmethod
    def from_core(cls, result: Any) -> "FixResultOut":
        return cls(
            name=result.name,
            executed=result.executed,
            command=result.command,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            message=result.message,
        )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ModelInfoOut(BaseModel):
    name: str
    path: str
    format: str = "unknown"
    size_bytes: int = 0
    size_gb: float = 0.0
    is_embedding: bool = False
    quantization: str | None = None
    torch_dtype: str | None = None
    max_position_embeddings: int | None = None
    architectures: list[str] = Field(default_factory=list)
    model_type: str | None = None
    tool_call_capable: bool = False
    multimodal: bool = False
    modalities: list[str] = Field(default_factory=list)
    is_moe: bool = False
    num_experts: int | None = None
    num_experts_per_tok: int | None = None
    param_count: int | None = None
    hidden_size: int | None = None
    num_hidden_layers: int | None = None
    engine_hint: str = "unknown"
    gguf_multi_shard: bool = False
    gguf_vllm_compatible: bool | None = None
    warnings: list[str] = Field(default_factory=list)
    valid: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    load_status: Literal["unloaded", "loading", "running", "error"] = "unloaded"

    @classmethod
    def from_core(cls, info: Any, load_status: str = "unloaded") -> "ModelInfoOut":
        return cls(
            name=info.name,
            path=info.path,
            format=info.format,
            size_bytes=info.size_bytes,
            size_gb=round(info.size_bytes / (1024 ** 3), 2) if info.size_bytes else 0.0,
            is_embedding=info.is_embedding,
            quantization=info.quantization,
            torch_dtype=getattr(info, "torch_dtype", None),
            max_position_embeddings=info.max_position_embeddings,
            architectures=list(info.architectures),
            model_type=getattr(info, "model_type", None),
            tool_call_capable=info.tool_call_capable,
            multimodal=getattr(info, "multimodal", False),
            modalities=list(getattr(info, "modalities", [])),
            is_moe=getattr(info, "is_moe", False),
            num_experts=getattr(info, "num_experts", None),
            num_experts_per_tok=getattr(info, "num_experts_per_tok", None),
            param_count=getattr(info, "param_count", None),
            hidden_size=getattr(info, "hidden_size", None),
            num_hidden_layers=getattr(info, "num_hidden_layers", None),
            engine_hint=info.engine_hint,
            gguf_multi_shard=info.gguf_multi_shard,
            gguf_vllm_compatible=info.gguf_vllm_compatible,
            warnings=list(info.warnings),
            valid=getattr(info, "valid", True),
            validation_errors=list(getattr(info, "validation_errors", [])),
            load_status=load_status,  # type: ignore[arg-type]
        )


class ModelListResponse(BaseModel):
    model_root_dir: str
    total: int
    models: list[ModelInfoOut]
    # Unix timestamp of the persisted scan this list came from (None if never
    # scanned). Lets the UI show "上次扫描时间" and a manual rescan button.
    scanned_at: float | None = None


class ParamEntryOut(BaseModel):
    default: Any = None
    editable: bool = True
    options: list[Any] | None = None
    source: str = ""


class GpuMemoryHintOut(BaseModel):
    """Advisory (non-binding) hint for the gpu_memory_utilization field, based
    on current free memory. vllm reserves gpu_memory_utilization × total at
    startup and fails if that exceeds free memory, so suggested_max ≈
    free/total tells the user how high they can safely go *right now* (e.g.
    with another model co-resident)."""

    mem_free_gb: float | None = None
    mem_total_gb: float | None = None
    suggested_max: float | None = None


class ParamsResponse(BaseModel):
    model_name: str
    engine: Literal["vllm"]
    params: dict[str, ParamEntryOut]
    gpu_memory_hint: GpuMemoryHintOut | None = None


class ModelLoadRequest(BaseModel):
    """User-confirmed launch configuration for a model.

    `engine` selects which param_advisor table was used to build `params`;
    `params` is a flat dict of {param_name: value} as edited by the user
    (already validated/defaulted on the frontend from /params response).
    """

    engine: Literal["vllm"] = "vllm"
    params: dict[str, Any] = Field(default_factory=dict)
    host_port: int | None = None


class ModelLoadResponse(BaseModel):
    model_name: str
    accepted: bool
    message: str
    compose_path: str | None = None
    command_result: dict[str, Any] | None = None


class ModelUnloadResponse(BaseModel):
    model_name: str
    accepted: bool
    message: str
    command_result: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

class ComponentOut(BaseModel):
    name: str
    container_name: str | None = None
    port: int | None = None
    # Bind address the port is published on, so the UI can show whether a
    # service is reachable from the LAN ("0.0.0.0", any interface) or only
    # from the server itself ("127.0.0.1"). None when the component has no
    # network port (shouldn't happen for the fixed rows, kept defensive).
    bind_host: str | None = None
    memory_usage_mb: float | None = None
    status: str = "unknown"
    detail: str = ""
    # False for the fixed infrastructure rows (Web/前端/SearXNG/未加载的模型槽位)
    # that POST /api/components/{name}/{action} can't act on (it only knows
    # how to start/stop/restart a model this backend's registry tracked as
    # loaded) — the frontend hides the start/stop/restart buttons for these.
    manageable: bool = True


class ComponentListResponse(BaseModel):
    components: list[ComponentOut]


class ComponentActionResponse(BaseModel):
    name: str
    action: Literal["start", "stop", "restart"]
    ok: bool
    message: str
    command_result: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

class LogsResponse(BaseModel):
    component: str
    lines: int
    content: str


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

class ModelLoadSummaryOut(BaseModel):
    """Top-of-page model load status block (general + embedding)."""

    general_models_loaded: list[str] = Field(default_factory=list)
    embedding_models_loaded: list[str] = Field(default_factory=list)
    # Models the registry started but whose container has since crashed/exited
    # (real docker state != running). Shown as "加载失败", never as "已加载".
    general_models_failed: list[str] = Field(default_factory=list)
    embedding_models_failed: list[str] = Field(default_factory=list)


class SystemResourcesOut(BaseModel):
    cpu_percent: float | None = None
    gpu_percent: float | None = None
    power_watts: float | None = None
    mem_used_gb: float | None = None
    mem_free_gb: float | None = None
    cache_gb: float | None = None


class OverviewResponse(BaseModel):
    model_load: ModelLoadSummaryOut
    env: EnvReportOut
    resources: SystemResourcesOut
    components: list[ComponentOut]


# ---------------------------------------------------------------------------
# Debug chat
# ---------------------------------------------------------------------------

class DebugChatRequest(BaseModel):
    model_name: str
    # base_url is no longer required from the frontend: debug_router now
    # resolves the real listening address via web.state.registry (the same
    # host_port recorded at model-load time), so a stale/incorrect
    # frontend-supplied base_url can no longer cause requests to go to the
    # wrong place. Kept optional for backward compatibility; ignored.
    base_url: str | None = None
    api_format: Literal["openai", "claude"] = "openai"
    system: str | None = None
    prompt: str
    max_tokens: int = 4096
    temperature: float = 0.7
    extra: dict[str, Any] = Field(default_factory=dict)


class DebugChatResponse(BaseModel):
    request_payload: dict[str, Any]
    status_code: int | None = None
    response_payload: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Performance test
#
# NOTE: this is a lightweight, project-defined concurrent throughput probe
# — it does NOT implement or claim conformance with any standardized LLM
# benchmark suite (e.g. MLPerf Inference, LLMPerf). Project_Task.md asks
# for "国际主流标准的检测规范" but integrating something like
# lm-eval-harness/MLPerf is a substantial separate effort out of scope for
# this phase; see 研发方案.md 阶段五 for the explicit call-out and a note
# on what a future standard-benchmark integration would require.
# ---------------------------------------------------------------------------

class PerfRunRequest(BaseModel):
    model_name: str
    concurrency: int = 4
    num_requests: int = 8
    prompt: str = "请用一段话介绍一下你自己。"
    max_tokens: int = 256
    temperature: float = 0.7
    stream: bool = False


class PerfRequestResult(BaseModel):
    index: int
    ok: bool
    status_code: int | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    # For failed requests only: the outgoing request (url + headers + body)
    # and the upstream response body, captured so a user can diagnose what
    # went wrong (e.g. 401/400 with the exact error payload) without digging
    # through logs. Left None on success to keep reports small.
    request_excerpt: dict | None = None
    response_excerpt: str | None = None


class PerfRunSummary(BaseModel):
    total_requests: int
    successful: int
    failed: int
    total_duration_ms: float
    avg_total_ms: float | None = None
    avg_ttft_ms: float | None = None
    total_completion_tokens: int
    throughput_tokens_per_sec: float | None = None


class PerfReportOut(BaseModel):
    report_id: str
    created_at: float
    model_name: str
    concurrency: int
    num_requests: int
    max_tokens: int
    summary: PerfRunSummary
    results: list[PerfRequestResult] = Field(default_factory=list)


class PerfReportListItem(BaseModel):
    report_id: str
    created_at: float
    model_name: str
    concurrency: int
    num_requests: int
    summary: PerfRunSummary


class PerfReportListResponse(BaseModel):
    reports: list[PerfReportListItem]


# ---------------------------------------------------------------------------
# Metrics history
# ---------------------------------------------------------------------------

class MetricsHistoryPoint(BaseModel):
    ts: float
    cpu_percent: float | None = None
    mem_used_gb: float | None = None
    mem_free_gb: float | None = None
    cache_gb: float | None = None
    gpu_percent: float | None = None
    gpu_temp_c: float | None = None
    power_watts: float | None = None
    # 每模型 tok/s 快照：{模型名: {prefill_tps, decode_tps, prompt_tokens_total,
    # generation_tokens_total}}。供运行观测的模型级 tok/s 趋势图（与系统级同源）。
    model_tps: dict | None = None


class MetricsHistoryResponse(BaseModel):
    window: str
    points: list[MetricsHistoryPoint]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsOut(BaseModel):
    web_port: int
    web_host: str
    admin_username: str
    model_root_dir: str
    data_dir: str
    searxng_port: int
    searxng_url: str
    vllm_image: str
    cuda_compat_dir: str
    searxng_proxy_url: str | None = None
    frontend_port: int
    # Shown in plaintext per project requirement (single-admin internal
    # tool, no multi-tenant secrecy boundary to protect against).
    secret_key: str
    admin_password: str
    vllm_api_key: str


class SettingsUpdateRequest(BaseModel):
    web_port: int | None = None
    web_host: str | None = None
    admin_username: str | None = None
    admin_password: str | None = None
    model_root_dir: str | None = None
    data_dir: str | None = None
    searxng_port: int | None = None
    searxng_url: str | None = None
    vllm_image: str | None = None
    cuda_compat_dir: str | None = None
    searxng_proxy_url: str | None = None
    frontend_port: int | None = None
    vllm_api_key: str | None = None
    rotate_secret_key: bool = False


# ---------------------------------------------------------------------------
# SearXNG
# ---------------------------------------------------------------------------

class SearxngStatusResponse(BaseModel):
    running: bool
    command_result: dict[str, Any] | None = None


class SearxngActionResponse(BaseModel):
    ok: bool
    message: str
    command_result: dict[str, Any] | None = None


class SearxngSearchResponse(BaseModel):
    ok: bool
    query: str
    data: dict[str, Any] | None = None
    error: str | None = None


class SearxngProxyTestRequest(BaseModel):
    proxy: str | None = None


class SearxngProxyTestResponse(BaseModel):
    ok: bool
    latency_ms: float | None = None
    status_code: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# WebSocket envelope (documented for the frontend, not strictly validated)
# ---------------------------------------------------------------------------

class WsEnvelope(BaseModel):
    topic: str
    data: dict[str, Any]
    ts: float
