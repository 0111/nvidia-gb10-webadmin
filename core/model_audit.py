"""Consolidated model audit.

Combines the static scan result (core.model_scanner.ModelInfo) with the
recommended vllm launch parameters (core.param_advisor) into one ordered
audit covering every object the operator wants to review before/after
loading a model:

    config 是否存在、tokenizer 是否存在/完整、权重 shard 是否完整、
    模型结构参数、量化配置、上下文长度、模型名/路径/架构/类型、
    权重大小、量化方式、最大上下文、vLLM 是否支持、推荐启动参数
    (max_model_len / max_num_seqs / max_num_batched_tokens /
     gpu_memory_utilization)、启动测试结果、错误摘要。

This module sits ABOVE both model_scanner and param_advisor (it imports
both) to avoid the circular import that would arise if model_scanner tried
to call param_advisor directly.
"""
from __future__ import annotations

from pathlib import Path

from core import param_advisor
from core.model_scanner import ModelInfo


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def _config_present(info: ModelInfo) -> bool:
    return (Path(info.path) / "config.json").is_file()


def _format_param_count(info: ModelInfo) -> str:
    """Human parameter count. Quantized checkpoints count packed on-disk
    tensors, so the number reads lower than the logical size — flag that."""
    if not info.param_count:
        return "--"
    n = info.param_count
    if n >= 1e9:
        val = f"{n / 1e9:.1f}B"
    elif n >= 1e6:
        val = f"{n / 1e6:.1f}M"
    else:
        val = str(n)
    if info.quantization:
        return f"约 {val}（量化打包后张量计数，逻辑参数量更高）"
    return f"约 {val}"


def _format_moe(info: ModelInfo) -> str:
    if not info.is_moe:
        return "否（Dense 稠密模型）"
    parts = []
    if info.num_experts:
        parts.append(f"{info.num_experts} 个专家")
    if info.num_experts_per_tok:
        parts.append(f"每 token 激活 {info.num_experts_per_tok} 个")
    return "是（MoE 混合专家" + ("，" + "、".join(parts) if parts else "") + "）"


def build_audit(info: ModelInfo) -> dict:
    """Build the consolidated audit dict for one model.

    Keys are stable English identifiers (for the API); each carries a
    Chinese `label` for direct display. `rows` is an ordered list suitable
    for rendering as an audit table.
    """
    rec = param_advisor.recommend_vllm_params(info)

    def rec_default(key: str):
        entry = rec.get(key)
        return entry.get("default") if entry else None

    config_ok = _config_present(info)
    # vLLM support: GGUF carries an explicit compatibility flag; safetensors
    # is supported as long as it passed static validation and has a tokenizer.
    if info.format == "gguf":
        if info.gguf_multi_shard:
            vllm_supported = "否（多分片GGUF，需先合并）"
        elif info.gguf_vllm_compatible is False:
            vllm_supported = "否（量化类型不被vllm支持）"
        elif info.gguf_vllm_compatible is True:
            vllm_supported = "是"
        else:
            vllm_supported = "未知"
    elif info.format == "safetensors":
        vllm_supported = "是" if info.valid else "否（静态校验未通过）"
    else:
        vllm_supported = "否（未识别格式）"

    startup_expectation = (
        "静态校验通过，预期可正常加载" if info.valid
        else "静态校验未通过，预期无法加载（见错误摘要）"
    )
    error_summary = "；".join(info.validation_errors) if info.validation_errors else ""

    modality_cn = {"image": "图片", "video": "视频", "audio": "音频"}
    modalities_display = (
        "、".join(modality_cn.get(m, m) for m in info.modalities)
        if info.modalities else "无（纯文本）"
    )
    supports_image = "image" in info.modalities
    # Multimodal note: an image expands to many tokens and its embedding can't
    # be split across prefill chunks, so max_model_len / max_num_batched_tokens
    # must comfortably exceed a single image's token count or image requests
    # fail while text ones succeed. Surface that hint right on the audit.
    multimodal_note = (
        f"是（{modalities_display}）；图片请求需 max_model_len 与 max_num_batched_tokens "
        f"足够大以容纳单张图片的视觉 token（当前推荐 batched={rec_default('max_num_batched_tokens')}，"
        f"图片分析建议调高）" if info.multimodal else "否"
    )

    rows = [
        ("name", "模型名", info.name),
        ("path", "模型路径", info.path),
        ("format", "格式", info.format),
        ("architectures", "架构", info.architectures),
        ("model_type", "模型类型(哪种模型)", info.model_type),
        ("param_count", "参数量", _format_param_count(info)),
        ("is_moe", "是否 MoE(混合专家)", _format_moe(info)),
        ("num_experts", "专家总数", info.num_experts),
        ("num_experts_per_tok", "每 token 激活专家数", info.num_experts_per_tok),
        ("hidden_size", "隐藏维度(hidden_size)", info.hidden_size),
        ("num_hidden_layers", "层数(num_hidden_layers)", info.num_hidden_layers),
        ("is_embedding", "是否嵌入模型", info.is_embedding),
        ("multimodal", "是否支持多模态", multimodal_note),
        ("supports_image", "是否支持图片分析", "是" if supports_image else "否"),
        ("modalities", "支持的输入模态", modalities_display),
        ("tool_call_capable", "是否支持工具功能(function calling)", "是" if info.tool_call_capable else "否"),
        ("config_present", "config 是否存在", config_ok),
        ("tokenizer_present", "tokenizer 是否存在/完整", info.tokenizer_present),
        ("tokenizer_files", "tokenizer 文件", info.tokenizer_files),
        ("weight_shards_complete", "权重 shard 是否完整",
         not any("分片" in e or "shard" in e.lower() or "权重清单" in e for e in info.validation_errors)),
        ("quantization", "量化方式", info.quantization),
        ("torch_dtype", "模型结构参数(torch_dtype)", info.torch_dtype),
        ("max_position_embeddings", "最大上下文长度", info.max_position_embeddings),
        ("weight_size", "权重大小", _human_size(info.size_bytes)),
        ("weight_size_bytes", "权重大小(字节)", info.size_bytes),
        ("vllm_supported", "vLLM 是否支持", vllm_supported),
        ("rec_max_model_len", "推荐 max_model_len", rec_default("max_model_len")),
        ("rec_max_num_seqs", "推荐 max_num_seqs", rec_default("max_num_seqs")),
        ("rec_max_num_batched_tokens", "推荐 max_num_batched_tokens", rec_default("max_num_batched_tokens")),
        ("rec_gpu_memory_utilization", "推荐 gpu_memory_utilization", rec_default("gpu_memory_utilization")),
        ("valid", "静态校验通过", info.valid),
        ("startup_expectation", "启动测试结果", startup_expectation),
        ("error_summary", "错误摘要", error_summary),
    ]

    return {
        "name": info.name,
        "valid": info.valid,
        "fields": {key: value for key, _label, value in rows},
        "rows": [{"key": key, "label": label, "value": value} for key, label, value in rows],
    }
