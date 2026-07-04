"""vllm launch parameter recommendation.

Translates a ModelInfo into a structured parameter recommendation dict
that the Web UI can render directly as a form: each parameter carries its
default value, whether it is user-editable, whether/what dropdown options
apply, and a short explanation of where the default came from.

All local models (safetensors and GGUF alike) are loaded through vllm —
see core/model_scanner.py's engine_hint and VLLM_SUPPORTED_GGUF_TYPES for
why GGUF no longer routes to a separate ollama engine.
"""
from __future__ import annotations

from pathlib import Path

from core.model_scanner import ModelInfo

# 4k/8k/16k/32k/64k/80k/96k/128k/144k/160k/192k/256k/512k/768k/1024k — fixed
# dropdown ladder for --max-model-len AND --max-num-batched-tokens (both
# rendered as <select>). Not auto-generated, so the displayed options always
# match this exact list; each is then capped per model at its
# max_position_embeddings (see _context_len_options), so a 1M-context model
# (e.g. Qwythos-9B-Claude-Mythos-5-1M, max_position_embeddings=1,048,576) shows
# up to 1024k while a 128k model tops out at 128k.
CONTEXT_LEN_OPTIONS = [
    4096, 8192, 16384, 32768, 65536, 81920, 98304,
    131072, 147456, 163840, 196608, 262144,
    524288, 786432, 1048576,
]
DEFAULT_MAX_MODEL_LEN_GENERAL = 65536  # 64k
DEFAULT_MAX_MODEL_LEN_EMBEDDING = 4096  # 4k

MAX_NUM_SEQS_OPTIONS = [1, 2, 3, 4]
# Curated subset of vllm 0.22's `--kv-cache-dtype` choices (full list also
# includes fp8_inc / fp8_per_token_head / int8_per_token_head / turboquant_*
# variants we don't surface). `nvfp4` added for the Blackwell/GB10 + NVFP4
# model fleet this tool targets — confirmed valid in 26.06-py3 (vllm 0.22.1).
KV_CACHE_DTYPE_OPTIONS = ["auto", "fp8", "fp8_e4m3", "fp8_e5m2", "nvfp4"]
DTYPE_OPTIONS = ["auto", "bfloat16", "float16", "float32"]
# vllm 0.22 `--tokenizer-mode` choices: {auto, deepseek_v32, deepseek_v4,
# hf, mistral, slow}; `hf` added vs 0.21.
TOKENIZER_MODE_OPTIONS = ["auto", "hf", "slow", "mistral", "deepseek_v32", "deepseek_v4"]

# The real registered method names (vllm.model_executor.layers.quantization
# .QUANTIZATION_METHODS, read from the actual vllm 0.22.1 image on the
# server, nvcr.io/nvidia/vllm:26.06-py3) — confirmed "NVFP4" is *not* one of
# these despite that being the string this project's model_scanner guesses
# from directory names like "...-NVFP4". See QUANTIZATION_OPTIONS' None-default
# comment below for why that guess must never be passed straight through as
# --quantization. (vs vllm 0.21: 0.22.1 added `auto_gptq`.)
QUANTIZATION_OPTIONS = [
    None, "awq", "awq_marlin", "auto_gptq", "bitsandbytes", "compressed-tensors", "cpu_awq",
    "deepseek_v4_fp8", "experts_int8", "fbgemm_fp8", "fp8", "fp8_per_block",
    "fp8_per_tensor", "fp_quant", "gguf", "gpt_oss_mxfp4", "gptq", "gptq_marlin",
    "humming", "inc", "int8_per_channel_weight_only", "modelopt", "modelopt_fp4",
    "modelopt_mixed", "modelopt_mxfp8", "moe_wna16", "mxfp4", "mxfp8", "online",
    "quark", "torchao",
]

# Full --tool-call-parser choice list, read from
# `vllm serve --help=tool-call-parser` on the real ARM64 server
# (nvcr.io/nvidia/vllm:26.06-py3, vllm 0.22.1). vs 0.21: 0.22.1 added `apertus`.
TOOL_CALL_PARSER_OPTIONS = [
    "apertus", "cohere_command3", "cohere_command4", "deepseek_v3", "deepseek_v31", "deepseek_v32",
    "deepseek_v4", "ernie45", "functiongemma", "gemma4", "gigachat3", "glm45", "glm47",
    "granite", "granite-20b-fc", "granite4", "hermes", "hunyuan_a13b", "hy_v3", "internlm",
    "jamba", "kimi_k2", "lfm2", "llama3_json", "llama4_json", "llama4_pythonic", "longcat",
    "mimo", "minimax", "minimax_m2", "mistral", "olmo3", "openai", "phi4_mini_json",
    "poolside_v1", "pythonic", "qwen3_coder", "qwen3_xml", "seed_oss", "step3", "step3p5", "xlam",
]

# architecture name (lowercased, substring match) -> likely tool_call_parser.
# Best-effort guess only; always user-editable/overridable in the UI.
_ARCH_TOOL_PARSER_HINTS: list[tuple[str, str]] = [
    ("qwen3", "qwen3_xml"),
    ("qwen2", "hermes"),
    ("qwen", "hermes"),
    ("llama4", "llama4_json"),
    ("llama3", "llama3_json"),
    ("llama", "llama3_json"),
    ("glm4_moe", "glm47"),
    ("glm4", "glm45"),
    ("glm", "glm45"),
    ("gemma", "gemma4"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("deepseekv3", "deepseek_v3"),
    ("deepseek_v2", "deepseek_v3"),
    ("deepseek", "deepseek_v3"),
    ("internlm", "internlm"),
    ("granite", "granite"),
    ("jamba", "jamba"),
    ("minimax", "minimax"),
    ("gptoss", "openai"),
    ("gpt_oss", "openai"),
]


def _guess_tool_call_parser(model_info: ModelInfo) -> str | None:
    """Pick the --tool-call-parser for a model.

    Prefer detecting the actual tool-call FORMAT from the model's chat
    template — far more reliable than guessing by name (e.g. all "qwen3" is
    NOT one format: Qwen3-Coder emits Qwen XML `<function=NAME>...` → qwen3_xml,
    while Qwen3 / Qwen3-VL emit Hermes-style `<tool_call>\n{"name":...}\n
    </tool_call>` → hermes). Using the wrong parser makes vllm emit malformed
    tool calls (name=None), which then fail validation when echoed back.
    Falls back to architecture/name hints when the template is unavailable.
    """
    try:
        tmpl = Path(model_info.path) / "chat_template.jinja"
        if tmpl.is_file():
            text = tmpl.read_text(encoding="utf-8", errors="ignore")
            if "<function=" in text:      # Qwen XML function-call format
                return "qwen3_xml"
            if "<tool_call>" in text:     # Hermes JSON-in-tags format
                return "hermes"
    except OSError:
        pass

    haystack = " ".join(model_info.architectures).lower() + " " + model_info.name.lower()
    for needle, parser in _ARCH_TOOL_PARSER_HINTS:
        if needle in haystack:
            return parser
    return None


def _param(default, editable: bool, options: list | None, source: str) -> dict:
    """Build one parameter entry in the shape the Web UI expects."""
    return {
        "default": default,
        "editable": editable,
        "options": options,
        "source": source,
    }


def _context_len_options(model_max_len: int | None) -> list[int]:
    """Dropdown options for max-model-len/max-num-batched-tokens, capped by the
    model's own max_position_embeddings if known."""
    if model_max_len is None:
        return CONTEXT_LEN_OPTIONS
    capped = [v for v in CONTEXT_LEN_OPTIONS if v <= model_max_len]
    return capped or [model_max_len]


def _default_max_model_len(context_options: list[int], desired: int) -> int | None:
    """Pick the dropdown entry closest to (but not exceeding) `desired`,
    falling back to the largest available entry if the model's own context
    cap is smaller than `desired`."""
    if not context_options:
        return None
    candidates = [v for v in context_options if v <= desired]
    return max(candidates) if candidates else min(context_options)


def recommend_vllm_params(model_info: ModelInfo) -> dict:
    """Recommend vllm launch parameters for a single model.

    Covers the parameter list from Project_Task.md 2.3.3: served-model-name,
    gpu-memory-utilization, max-model-len, max-num-seqs,
    max-num-batched-tokens, enable-auto-tool-choice, tool-call-parser,
    chat-template, enable-chunked-prefill, enable-prefix-caching,
    trust-remote-code, kv-cache-memory-bytes, kv-cache-dtype, tokenizer,
    dtype, quantization, generation-config, reasoning-parser,
    tokenizer-mode, task, runner.
    """
    model_max_len = model_info.max_position_embeddings
    context_options = _context_len_options(model_max_len)
    desired_default = (
        DEFAULT_MAX_MODEL_LEN_EMBEDDING if model_info.is_embedding else DEFAULT_MAX_MODEL_LEN_GENERAL
    )
    default_max_model_len = _default_max_model_len(context_options, desired_default)
    default_batched_tokens = min(8192, default_max_model_len or 8192)
    if default_batched_tokens not in context_options and context_options:
        default_batched_tokens = context_options[0]

    chat_template_path = None
    candidate = f"{model_info.path}/chat_template.jinja"
    chat_template_path = candidate  # presence not re-checked here; UI can validate

    # Tool-calling only applies to generative models. Embedding/pooling models
    # sometimes ship a chat template mentioning "tools" (→ tool_call_capable),
    # but launching a `runner=pooling` model with --enable-auto-tool-choice is
    # nonsensical and can fail at startup — so gate tool params on NOT embedding.
    tool_capable = model_info.tool_call_capable and not model_info.is_embedding
    guessed_tool_parser = _guess_tool_call_parser(model_info) if tool_capable else None

    params: dict[str, dict] = {
        "served_model_name": _param(
            model_info.name, True, None, "默认取模型目录名"),
        "gpu_memory_utilization": _param(
            0.90, True, None, "默认 0.90，最大不超过 0.95（用户可调）"),
        "max_model_len": _param(
            default_max_model_len, True, context_options,
            ("默认 4k（嵌入模型）" if model_info.is_embedding else "默认 64k（通用模型）")
            + "，按下拉清单就近取值，且不超过 config.json max_position_embeddings"),
        "max_num_seqs": _param(
            1, True, MAX_NUM_SEQS_OPTIONS, "默认 1，可选 1-4"),
        "max_num_batched_tokens": _param(
            default_batched_tokens, True, context_options,
            "默认 8k（或不超过模型上限的最接近档位）"),
        # Default ON for tool-capable models that have a guessable parser:
        # OpenAI-compatible clients (OpenWebUI, CherryStudio, …) send
        # `tool_choice: "auto"` by default, and vllm HARD-ERRORS that request
        # ("auto" tool choice requires --enable-auto-tool-choice and
        # --tool-call-parser to be set) unless the server was launched with
        # both flags. Enabling it is safe for plain chat/vision requests (the
        # parser only activates when the model actually emits a tool call), so
        # defaulting it on removes a very common "works in curl, fails in my
        # chat UI" foot-gun. Left OFF only when no parser could be guessed
        # (can't enable without a valid --tool-call-parser) or the model isn't
        # tool-capable.
        "enable_auto_tool_choice": _param(
            bool(tool_capable and guessed_tool_parser),
            tool_capable, None,
            "默认勾选（模型支持工具且已推断解析器）——OpenAI 兼容客户端默认发 "
            "tool_choice=auto，未开启会被 vllm 直接拒绝；对纯聊天/图片请求无副作用"
            if (tool_capable and guessed_tool_parser) else
            ("模型支持工具但未能推断解析器，请手动选择 tool_call_parser 后再勾选"
             if tool_capable else "该模型不适用工具调用（嵌入模型或未检测到能力）")),
        "tool_call_parser": _param(
            guessed_tool_parser, tool_capable,
            TOOL_CALL_PARSER_OPTIONS,
            "勾选 enable_auto_tool_choice 后必填；按模型架构猜测默认值，完整可选列表来自 "
            "`vllm serve --help=Frontend` 的 --tool-call-parser choices，用户可改"),
        "chat_template": _param(
            None, True, None,
            f"默认不设置，可选择模型自带模板: {chat_template_path}"),
        "enable_chunked_prefill": _param(True, True, None, "默认勾选"),
        "enable_prefix_caching": _param(True, True, None, "默认勾选"),
        "trust_remote_code": _param(False, True, None, "默认不勾选"),
        "enforce_eager": _param(
            False, True, None,
            "默认不勾选。勾选后关闭 CUDA graph、用 eager 模式执行——速度略降，"
            "但可规避个别量化(NVFP4等)模型 CUDA graph 重放导致的数值异常"
            "（输出满屏 ! 或引擎卡死）。若模型偶发输出乱码/卡死可勾选试试。"),
        "kv_cache_memory_bytes": _param(None, True, None, "默认不设置"),
        "kv_cache_dtype": _param(
            "auto", True, KV_CACHE_DTYPE_OPTIONS, "默认 auto"),
        "tokenizer": _param(
            None, True, None, "默认不设置，沿用模型自带 tokenizer"),
        "dtype": _param("auto", True, DTYPE_OPTIONS, "默认 auto"),
        # Default None (omit --quantization entirely) rather than the
        # quantization *label* model_scanner detected for display purposes
        # (e.g. "NVFP4" guessed from a directory name, or "GGUF Q8_0").
        # Those labels are informational only and are NOT valid
        # --quantization values — confirmed against vllm 0.22.1's real
        # QUANTIZATION_METHODS registry, "NVFP4" isn't even in that list
        # (real NVIDIA FP4 method names are "modelopt_fp4"/"compressed-tensors"
        # depending on the checkpoint's quantization_config.format). vllm's
        # own --help text says: "If `None`, we first check the
        # `quantization_config` attribute in the model config file" — i.e.
        # auto-detection from config.json is the documented, correct
        # default, and forcing a guessed/wrong string causes a hard
        # "unknown quantization method" failure at startup instead.
        "quantization": _param(
            None, True, QUANTIZATION_OPTIONS,
            f"默认不设置(自动从config.json的quantization_config检测)；"
            + (f"模型目录名/元数据显示为「{model_info.quantization}」仅供参考，"
               "并非有效的--quantization取值，请勿直接照抄" if model_info.quantization else "")),
        "generation_config": _param(
            None, True, None, "默认不设置，可选择模型自带 generation_config.json"),
        "reasoning_parser": _param(
            None, True, None, "默认不设置，需根据模型架构进一步识别"),
        "tokenizer_mode": _param(
            "auto", True, TOKENIZER_MODE_OPTIONS, "默认 auto"),
        # NOTE: vllm 0.21+ removed the old `--task` flag in favor of
        # `--convert` (adapts a text-generation checkpoint for pooling
        # tasks) plus `--runner`. Passing `--task embed` to this vllm
        # version raises "unrecognized arguments" and the container exits
        # immediately — re-verified on vllm 0.22.1 (26.06-py3): `--task`
        # is still gone, `--convert {auto,classify,embed,none}` +
        # `--runner {auto,draft,generate,pooling}` are the real flags.
        "convert": _param(
            "embed" if model_info.is_embedding else "auto", False if model_info.is_embedding else True,
            ["auto", "embed", "classify", "none"],
            "embedding 模型强制 --convert embed" if model_info.is_embedding else "默认 auto，由 vllm 自行推断"),
        "runner": _param(
            "pooling" if model_info.is_embedding else "generate", True,
            ["auto", "draft", "generate", "pooling"],
            "embedding 模型默认 pooling，通用模型默认 generate"),
    }

    return params
