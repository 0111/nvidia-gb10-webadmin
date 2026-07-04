"""Local model directory scanner.

Recursively walks the (read-only) model root directory and identifies model
"leaf" directories: any directory directly containing a config.json or at
least one .gguf file is treated as a single model. Parsing is best-effort —
a malformed or missing config.json degrades to a partially-filled
ModelInfo plus a warning, it never aborts the whole scan.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

EMBEDDING_KEYWORDS = ("embedding", "embed", "reranker", "rerank")
GGUF_SUFFIX = ".gguf"
SAFETENSORS_SUFFIX = ".safetensors"
GGUF_SHARD_PATTERN = re.compile(r"-(\d+)-of-(\d+)\.gguf$", re.IGNORECASE)
# HF-format sharded safetensors: "model-00001-of-00004.safetensors". The
# "-of-N" suffix declares the total shard count, so a complete model must
# have all N consecutively-numbered files present — used by _validate_model
# to detect the exact "missing shard" case (the user's broken
# Qwen3-VL-30B-...-NVFP4 had shard 2 of 4 missing).
SAFETENSORS_SHARD_PATTERN = re.compile(r"^(.*)-(\d+)-of-(\d+)\.safetensors$", re.IGNORECASE)

# vllm 0.22's GGUF quantization layer (model_executor/layers/quantization/gguf.py)
# only dequantizes these GGML tensor types (UNQUANTIZED_TYPES | STANDARD_QUANT_TYPES
# | KQUANT_TYPES | IMATRIX_QUANT_TYPES). Anything else (MXFP4/NVFP4 used by some
# newer GGUF exports, TQ*/Q1_0 ternary types, raw int types) raises
# NotImplementedError at load time — confirmed by reading that file on the
# server's nvcr.io/nvidia/vllm:26.06-py3 image (vllm 0.22.1; the
# UNQUANTIZED/STANDARD/KQUANT/IMATRIX type sets are unchanged vs 0.21).
VLLM_SUPPORTED_GGUF_TYPES = {
    "F32", "F16", "BF16",
    "Q4_0", "Q4_1", "Q5_0", "Q5_1", "Q8_0", "Q8_1",
    "Q2_K", "Q3_K", "Q4_K", "Q5_K", "Q6_K",
    "IQ1_M", "IQ1_S", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ3_XXS", "IQ3_S", "IQ4_XS", "IQ4_NL",
}


@dataclass
class ModelInfo:
    """Describes a single discovered local model.

    Attributes:
        name: directory name, used as the default --served-model-name.
        path: absolute path to the model directory.
        format: "safetensors" | "gguf" | "unknown".
        size_bytes: total size of all model weight files in the directory.
        is_embedding: True if path/name or config suggests an embedding
            or reranker model.
        quantization: e.g. "NVFP4", "FP8", "GGUF Q8_0", or None.
        max_position_embeddings: context length upper bound from config.json.
        architectures: list of architecture class names from config.json.
        tool_call_capable: True if a *tool_parser*.py file exists or the
            chat template references tool-call keywords.
        engine_hint: "vllm" for both safetensors and gguf (vllm 0.22+ has a
            native, if experimental, GGUF loader — see VLLM_SUPPORTED_GGUF_TYPES);
            "unknown" otherwise.
        gguf_multi_shard: True if the model directory holds a llama.cpp-style
            split GGUF (e.g. "*-00001-of-00009.gguf") rather than a single
            file. vllm's GGUF loader only supports single-file models — use
            tools/gguf_merge_shards.py to merge before loading.
        gguf_vllm_compatible: For single-file GGUF models, whether every
            tensor's GGML quant type is one vllm's GGUF loader can dequantize.
            None if not a GGUF model or compatibility couldn't be determined
            (e.g. the `gguf` package isn't installed locally).
        warnings: list of human-readable issues found while parsing.
    """

    name: str
    path: str
    format: str = "unknown"
    size_bytes: int = 0
    is_embedding: bool = False
    quantization: str | None = None
    # torch_dtype from config.json (e.g. "bfloat16"/"float16"). Used by the
    # UI to show something meaningful for *non*-quantized models, where
    # quantization is None — "--" alone reads like missing/broken data, so
    # 运行观测 shows e.g. "未量化（bfloat16）" instead.
    torch_dtype: str | None = None
    max_position_embeddings: int | None = None
    architectures: list[str] = field(default_factory=list)
    # model_type from config.json (e.g. "qwen3", "llama", "bert"). Distinct
    # from `architectures` (the class names) — used in the audit view.
    model_type: str | None = None
    # Tokenizer presence/completeness (safetensors models need tokenizer
    # files alongside the weights; GGUF embeds its own). tokenizer_files
    # lists which recognized tokenizer artifacts were found in the dir.
    tokenizer_present: bool = False
    tokenizer_files: list[str] = field(default_factory=list)
    tool_call_capable: bool = False
    # Multimodal (vision-language / omni) support: `multimodal` is True when the
    # model accepts non-text inputs; `modalities` lists which ones ("image",
    # "video", "audio"). Detected from config.json (vision_config /
    # image_token_id / video_token_id / audio_config) + processor files. vllm
    # loads these as normal `generate` models — no extra flag to *enable*
    # images — but image inputs expand to many tokens, so max_model_len /
    # max_num_batched_tokens must be large enough (see _detect_multimodal).
    multimodal: bool = False
    modalities: list[str] = field(default_factory=list)
    # Architecture / scale auxiliary info for the audit view.
    #   is_moe / num_experts / num_experts_per_tok — Mixture-of-Experts shape
    #     (num_experts total, num_experts_per_tok active per token). Dense
    #     models leave these None / is_moe False.
    #   param_count — total parameter count (from the safetensors index's
    #     `total_parameters`, else summed from tensor headers). For quantized
    #     checkpoints (NVFP4/compressed-tensors) this counts the *packed*
    #     on-disk tensors, so it reads lower than the logical (unquantized)
    #     parameter count — treated as approximate.
    #   hidden_size / num_hidden_layers — key transformer dims.
    is_moe: bool = False
    num_experts: int | None = None
    num_experts_per_tok: int | None = None
    param_count: int | None = None
    hidden_size: int | None = None
    num_hidden_layers: int | None = None
    engine_hint: str = "unknown"
    gguf_multi_shard: bool = False
    gguf_vllm_compatible: bool | None = None
    warnings: list[str] = field(default_factory=list)
    # Integrity validation result (see _validate_model). valid=False means the
    # model is broken / cannot be loaded as-is (e.g. a safetensors shard is
    # missing, no weight files at all, GGUF quant unsupported by vllm). The
    # UI highlights these rows and the load endpoint refuses them. Distinct
    # from `warnings`, which are softer/non-blocking notes.
    valid: bool = True
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelInfo":
        """Rebuild a ModelInfo from its to_dict() form (used to load a
        persisted scan result), tolerating extra/missing keys."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def _is_model_dir(directory: Path) -> bool:
    if (directory / "config.json").is_file():
        return True
    return any(directory.glob(f"*{GGUF_SUFFIX}"))


def _detect_format(directory: Path) -> str:
    if any(directory.glob(f"*{SAFETENSORS_SUFFIX}")):
        return "safetensors"
    if any(directory.glob(f"*{GGUF_SUFFIX}")):
        return "gguf"
    return "unknown"


def _total_size(directory: Path, suffixes: tuple[str, ...]) -> int:
    total = 0
    for suffix in suffixes:
        for f in directory.glob(f"*{suffix}"):
            try:
                total += f.stat().st_size
            except OSError:
                continue
    return total


def _looks_like_embedding(directory: Path, config: dict) -> bool:
    haystack = str(directory).lower()
    if any(kw in haystack for kw in EMBEDDING_KEYWORDS):
        return True
    model_type = str(config.get("model_type", "")).lower()
    architectures = [str(a).lower() for a in config.get("architectures", [])]
    if any(kw in model_type for kw in EMBEDDING_KEYWORDS):
        return True
    return any(kw in arch for arch in architectures for kw in EMBEDDING_KEYWORDS)


def _detect_quantization(directory: Path, config: dict) -> str | None:
    name_upper = directory.name.upper()
    for tag in ("NVFP4", "FP8", "MXFP4", "INT4", "INT8", "AWQ", "GPTQ"):
        if tag in name_upper:
            return tag

    hf_quant_path = directory / "hf_quant_config.json"
    if hf_quant_path.is_file():
        try:
            hf_quant = json.loads(hf_quant_path.read_text(encoding="utf-8"))
            quant_algo = hf_quant.get("quant_algo") or hf_quant.get("quantization")
            if quant_algo:
                return str(quant_algo)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("解析 hf_quant_config.json 失败 %s: %s", directory, exc)

    quant_cfg = config.get("quantization_config")
    if isinstance(quant_cfg, dict):
        method = quant_cfg.get("quant_method") or quant_cfg.get("method")
        if method:
            return str(method).upper()

    gguf_files = list(directory.glob(f"*{GGUF_SUFFIX}"))
    if gguf_files:
        match_name = gguf_files[0].stem.upper()
        for tag in ("Q8_0", "Q8_K", "Q6_K", "Q5_K", "Q4_K", "Q4_0", "FP16", "MXFP4"):
            if tag in match_name:
                return f"GGUF {tag}"
        return "GGUF"

    return None


def _detect_tool_call_capable(directory: Path) -> bool:
    if any(directory.glob("*tool_parser*.py")):
        return True
    chat_template = directory / "chat_template.jinja"
    if chat_template.is_file():
        try:
            content = chat_template.read_text(encoding="utf-8", errors="ignore").lower()
            if "tool_call" in content or "tools" in content:
                return True
        except OSError:
            pass
    return False


def _detect_multimodal(directory: Path, config: dict) -> tuple[bool, list[str]]:
    """Detect whether a model is multimodal and which extra input modalities
    (image/video/audio) it accepts, from config.json + processor files.

    Each modality is gated on a *structural* signal, not a bare token id: many
    text/image models reserve `video_token_id`/`audio_token_id` vocab slots
    they don't actually use (e.g. Gemma-4 has both but only does image), so a
    token id alone over-reports. Reliable signals:
      - image: a `vision_config` sub-config, or a preprocessor_config.json
        declaring an image_processor_type;
      - video: a video_preprocessor_config.json file, or a `video_config` dict;
      - audio: an `audio_config` sub-config.
    """
    modalities: list[str] = []

    def add(m: str) -> None:
        if m not in modalities:
            modalities.append(m)

    if isinstance(config.get("vision_config"), dict):
        add("image")
    pre = directory / "preprocessor_config.json"
    if pre.is_file():
        try:
            pc = json.loads(pre.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(pc, dict) and (pc.get("image_processor_type") or pc.get("image_processor")):
                add("image")
        except (OSError, ValueError):
            pass

    if (directory / "video_preprocessor_config.json").is_file() or isinstance(config.get("video_config"), dict):
        add("video")

    if isinstance(config.get("audio_config"), dict):
        add("audio")

    # Stable, human-friendly order.
    ordered = [m for m in ("image", "video", "audio") if m in modalities]
    return (len(ordered) > 0, ordered)


def _cfg_value(config: dict, *keys: str):
    """First non-None value for any of `keys`, checking the top-level config
    then a nested `text_config` (VL/omni configs put the LM dims there)."""
    text_cfg = config.get("text_config") if isinstance(config.get("text_config"), dict) else {}
    for source in (config, text_cfg):
        for key in keys:
            val = source.get(key)
            if val is not None:
                return val
    return None


def _detect_moe(config: dict) -> tuple[bool, int | None, int | None]:
    """Detect Mixture-of-Experts shape from config.json.

    Returns (is_moe, num_experts_total, num_experts_active_per_token). Covers
    the common key spellings across families (num_experts / n_routed_experts /
    num_local_experts, num_experts_per_tok / moe_topk / num_experts_per_token).
    """
    num_experts = _cfg_value(config, "num_experts", "n_routed_experts", "num_local_experts")
    per_tok = _cfg_value(config, "num_experts_per_tok", "num_experts_per_token", "moe_topk")
    num_experts = num_experts if isinstance(num_experts, int) else None
    per_tok = per_tok if isinstance(per_tok, int) else None

    arch_hint = (
        "moe" in (config.get("model_type") or "").lower()
        or any("moe" in str(a).lower() for a in (config.get("architectures") or []))
    )
    is_moe = bool((num_experts and num_experts > 1) or arch_hint)
    return is_moe, num_experts, per_tok


def _detect_param_count(directory: Path, config: dict) -> int | None:
    """Total parameter count. Prefers the safetensors index's declared
    `total_parameters`; otherwise sums tensor element counts from each
    safetensors file's JSON header (header-only read, not the weights). For
    quantized checkpoints this is the packed on-disk count (approximate)."""
    index = directory / "model.safetensors.index.json"
    if index.is_file():
        try:
            meta = json.loads(index.read_text(encoding="utf-8", errors="ignore")).get("metadata", {})
            tp = meta.get("total_parameters")
            if isinstance(tp, int) and tp > 0:
                return tp
        except (OSError, ValueError):
            pass

    total = 0
    found = False
    for st in sorted(directory.glob("*.safetensors")):
        try:
            with open(st, "rb") as fh:
                n = int.from_bytes(fh.read(8), "little")
                if n <= 0 or n > 100_000_000:  # sane header-size guard
                    continue
                header = json.loads(fh.read(n).decode("utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        for name, spec in header.items():
            if name == "__metadata__" or not isinstance(spec, dict):
                continue
            shape = spec.get("shape") or []
            if shape:
                count = 1
                for dim in shape:
                    count *= int(dim)
                total += count
                found = True
    return total if found else None


# scan_models() runs on essentially every API request (no caller caches its
# result), so this cache is what keeps repeated /api/overview, /api/models,
# /api/models/{name}/params calls fast. Opening gguf.GGUFReader on a 30-60GB
# GGUF file mmaps the whole file and was measured taking several seconds
# *per file*, per call, on the real ARM64 server — with 5 local GGUF models
# that alone pushed scan_models() past 30s and made every page using it
# (实时总览/模型选择列表) time out client-side. Keyed by (path, mtime, size)
# so an actually-changed file (re-downloaded/re-merged) is re-checked.
_GGUF_COMPAT_CACHE: dict[tuple[str, float, int], tuple[bool, list[str]]] = {}


def _detect_gguf_vllm_compat(directory: Path, info: ModelInfo) -> None:
    gguf_files = sorted(directory.glob(f"*{GGUF_SUFFIX}"))
    if not gguf_files:
        return

    if len(gguf_files) > 1 or GGUF_SHARD_PATTERN.search(gguf_files[0].name):
        info.gguf_multi_shard = True
        info.warnings.append(
            "多分片GGUF模型，vllm仅支持单文件GGUF，需先用 tools/gguf_merge_shards.py 合并为单文件后才能加载"
        )
        return

    gguf_file = gguf_files[0]
    try:
        stat = gguf_file.stat()
    except OSError:
        return
    cache_key = (str(gguf_file), stat.st_mtime, stat.st_size)
    cached = _GGUF_COMPAT_CACHE.get(cache_key)
    if cached is not None:
        info.gguf_vllm_compatible, unsupported = cached
        if unsupported:
            info.warnings.append(
                f"GGUF包含vllm 0.22 GGUF加载器不支持的量化类型 {unsupported}，无法通过vllm引擎加载"
            )
        return

    try:
        import gguf as gguf_pkg
    except ImportError:
        logger.debug("未安装 gguf 包，跳过GGUF量化类型兼容性检测: %s", directory)
        return

    try:
        reader = gguf_pkg.GGUFReader(str(gguf_file), "r")
        tensor_types = {t.tensor_type.name for t in reader.tensors}
        unsupported = sorted(tensor_types - VLLM_SUPPORTED_GGUF_TYPES)
        info.gguf_vllm_compatible = not unsupported
        _GGUF_COMPAT_CACHE[cache_key] = (info.gguf_vllm_compatible, unsupported)
        if unsupported:
            info.warnings.append(
                f"GGUF包含vllm 0.22 GGUF加载器不支持的量化类型 {unsupported}，无法通过vllm引擎加载"
            )
    except Exception as exc:
        logger.warning("GGUF兼容性检测失败 %s: %s", directory, exc)


def _parse_config_json(directory: Path, info: ModelInfo) -> dict:
    config_path = directory / "config.json"
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        info.warnings.append(f"config.json 解析失败: {exc}")
        return {}


def _missing_safetensors_shards(directory: Path) -> int:
    """Return how many safetensors weight shards are missing (0 = complete).

    Real-world HF exports are inconsistent about shard numbering — some are
    1-indexed ("model-00001-of-00008"), some 0-indexed
    ("model-00000-of-00004"), and some even pad the index and total
    differently ("model-00001-of-000008"). So reconstructing exact expected
    filenames is fragile and caused false positives. Instead we just compare
    the COUNT of present shard files against the total `N` declared in their
    "-of-N" suffix: a complete model has exactly N shard files regardless of
    how they're numbered/padded. Cheap (filename inspection only, no reading
    of the multi-GB weights or the multi-MB index.json).
    """
    shard_files = sorted(directory.glob(f"*-of-*{SAFETENSORS_SUFFIX}"))
    if not shard_files:
        return 0
    # Group present shard indices by their declared total; a directory should
    # really only have one such group, but pick the largest to be safe.
    by_total: dict[int, set[int]] = {}
    for f in shard_files:
        match = SAFETENSORS_SHARD_PATTERN.match(f.name)
        if not match:
            continue
        try:
            idx, total = int(match.group(2)), int(match.group(3))
        except ValueError:
            continue
        by_total.setdefault(total, set()).add(idx)
    if not by_total:
        return 0
    total = max(by_total, key=lambda t: len(by_total[t]))
    present = len(by_total[total])
    return max(0, total - present)


def _dir_signature(directory: Path) -> tuple:
    """Cheap fingerprint of a directory's contents: sorted (name, size, mtime)
    of every entry. Used as the validation cache key so the (relatively
    pricey) multi-dimension checks — notably parsing a multi-MB
    *.index.json — run once per actual on-disk state and are reused on
    repeat scans, but are correctly re-run the moment any file is
    added/removed/changed (e.g. a missing shard gets re-downloaded)."""
    entries = []
    try:
        with os.scandir(directory) as it:
            for e in it:
                try:
                    st = e.stat()
                    entries.append((e.name, st.st_size, int(st.st_mtime)))
                except OSError:
                    entries.append((e.name, -1, -1))
    except OSError:
        return ()
    return tuple(sorted(entries))


# Validation results cached by (dir path, dir signature) so scan_models stays
# fast on repeated calls; same rationale/pattern as _GGUF_COMPAT_CACHE.
_VALIDATION_CACHE: dict[tuple, tuple[bool, list[str]]] = {}


def _validate_safetensors_index(directory: Path, index_path: Path) -> list[str]:
    """Authoritative manifest cross-check using model.safetensors.index.json.

    The index's `weight_map` lists every shard file the model actually needs
    (regardless of how they're numbered/padded), so it's the correct source
    of truth for completeness — more reliable than guessing filenames. Also
    cross-checks the summed shard byte size against the index's
    metadata.total_size to catch truncated/partial downloads.
    """
    errors: list[str] = []
    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"权重索引 {index_path.name} 解析失败: {exc}"]

    weight_map = idx.get("weight_map") or {}
    referenced = sorted({str(v) for v in weight_map.values()})
    if not referenced:
        return errors

    missing = [s for s in referenced if not (directory / s).is_file()]
    if missing:
        preview = ", ".join(missing[:3]) + (" 等" if len(missing) > 3 else "")
        errors.append(f"按权重索引应有 {len(referenced)} 个分片，缺失 {len(missing)} 个（{preview}），模型不完整")
        return errors  # no point size-checking an incomplete set

    meta = idx.get("metadata") or {}
    total_size = meta.get("total_size")
    if isinstance(total_size, int) and total_size > 0:
        actual = 0
        for s in referenced:
            try:
                actual += (directory / s).stat().st_size
            except OSError:
                actual = -1
                break
        # Each .safetensors file = header bytes + tensor bytes, so the real
        # on-disk total is always >= total_size; a smaller total means data
        # is missing/truncated.
        if 0 <= actual < total_size:
            errors.append(
                f"权重实际总字节 {actual} 小于索引声明 total_size {total_size}，疑似文件被截断/未下载完整"
            )
    return errors


# Recognized tokenizer artifacts. A safetensors model needs at least one of
# the actual vocab carriers (tokenizer.json / *.model / vocab.json) for vllm
# to build a tokenizer; tokenizer_config.json alone is not enough.
_TOKENIZER_VOCAB_FILES = ("tokenizer.json", "tokenizer.model", "spiece.model", "vocab.json", "vocab.txt")
_TOKENIZER_AUX_FILES = ("tokenizer_config.json", "merges.txt", "special_tokens_map.json", "added_tokens.json")


def _detect_tokenizer(directory: Path) -> tuple[bool, list[str]]:
    """Return (has_usable_vocab, found_files) for tokenizer artifacts in dir.

    has_usable_vocab is True only if at least one real vocab carrier exists
    (tokenizer.json / *.model / vocab.*). Aux files (tokenizer_config.json,
    merges.txt, ...) are reported too but don't by themselves make a usable
    tokenizer.
    """
    found: list[str] = []
    has_vocab = False
    for fname in (*_TOKENIZER_VOCAB_FILES, *_TOKENIZER_AUX_FILES):
        if (directory / fname).is_file():
            found.append(fname)
            if fname in _TOKENIZER_VOCAB_FILES:
                has_vocab = True
    return has_vocab, found


def _validate_tokenizer(directory: Path) -> list[str]:
    """Tokenizer completeness for safetensors models. A missing tokenizer
    makes vllm fail at startup, so treat it as a hard error (matching the
    project's "catch it statically, don't let vllm crash opaquely" stance)."""
    has_vocab, found = _detect_tokenizer(directory)
    if not found:
        return ["缺少 tokenizer 文件（无 tokenizer.json/*.model/vocab.*），vllm 无法构建分词器"]
    if not has_vocab:
        return [f"tokenizer 不完整：仅有 {', '.join(found)}，缺少词表文件(tokenizer.json/*.model/vocab.*)"]
    return []


def _validate_safetensors(directory: Path, config: dict) -> list[str]:
    errors: list[str] = []

    # 维度1：配置文件解析
    config_path = directory / "config.json"
    if not config_path.is_file():
        errors.append("缺少 config.json，无法作为 safetensors 模型加载")
    elif not config:
        errors.append("config.json 存在但解析失败或为空")
    elif not config.get("architectures") and not config.get("model_type"):
        errors.append("config.json 缺少 architectures/model_type 关键字段")

    # 维度2：权重文件存在性
    weight_files = list(directory.glob(f"*{SAFETENSORS_SUFFIX}"))
    if not weight_files:
        errors.append("未找到任何 .safetensors 权重文件")
        return errors

    # 维度3：空/损坏文件
    for f in weight_files:
        try:
            if f.stat().st_size == 0:
                errors.append(f"权重文件为空(0字节)，疑似损坏: {f.name}")
        except OSError:
            errors.append(f"无法读取权重文件: {f.name}")

    # 维度4：清单完整性（优先用 index.json 权重清单，缺索引时回退分片计数）
    index_files = list(directory.glob(f"*{SAFETENSORS_SUFFIX}.index.json"))
    if index_files:
        errors.extend(_validate_safetensors_index(directory, index_files[0]))
    else:
        missing_count = _missing_safetensors_shards(directory)
        if missing_count:
            errors.append(f"权重分片缺失 {missing_count} 个（无索引文件，按 -of-N 总数比对），模型不完整")

    # 维度5：tokenizer 完整性（safetensors 模型需自带分词器文件）
    errors.extend(_validate_tokenizer(directory))
    return errors


def _validate_gguf(directory: Path, info: ModelInfo) -> list[str]:
    errors: list[str] = []
    gguf_files = list(directory.glob(f"*{GGUF_SUFFIX}"))
    if not gguf_files:
        errors.append("未找到任何 .gguf 权重文件")
        return errors
    for f in gguf_files:
        try:
            if f.stat().st_size == 0:
                errors.append(f"GGUF 文件为空(0字节)，疑似损坏: {f.name}")
        except OSError:
            errors.append(f"无法读取 GGUF 文件: {f.name}")
    # Multi-shard GGUF is fixable (merge first) → kept as a warning elsewhere,
    # not a hard error. Unsupported quant genuinely can't load via vllm.
    if info.gguf_vllm_compatible is False:
        errors.append("GGUF 量化类型不被当前 vllm 版本支持，无法加载")
    return errors


def _validate_model(directory: Path, info: ModelInfo, config: dict) -> None:
    """Multi-dimension static integrity check (no engine load involved),
    filling info.valid / info.validation_errors. Dimensions: 格式/后缀识别、
    config.json 解析与关键字段、权重文件存在性、空/损坏文件、以及用
    *.index.json 权重清单做的分片完整性 + total_size 大小核对。

    Result is cached by directory signature (see _dir_signature) so the
    index.json parse doesn't repeat on every scan. Optional SHA256 hash
    verification is intentionally NOT done here (it reads whole multi-GB
    files) — see verify_model_hashes(), invoked only on explicit request.
    """
    sig = (str(directory),) + _dir_signature(directory)
    cached = _VALIDATION_CACHE.get(sig)
    if cached is not None:
        info.valid, info.validation_errors = cached[0], list(cached[1])
        return

    if info.format == "safetensors":
        errors = _validate_safetensors(directory, config)
    elif info.format == "gguf":
        errors = _validate_gguf(directory, info)
    else:
        errors = ["无法识别模型格式（既非 safetensors 也非 gguf）"]

    info.validation_errors = errors
    info.valid = not errors
    _VALIDATION_CACHE[sig] = (info.valid, list(errors))


# ---------------------------------------------------------------------------
# Opt-in deep hash verification (NOT part of routine scan — reads whole files)
# ---------------------------------------------------------------------------

# Manifest filenames commonly shipped alongside model weights. Each line is
# "<sha256>  <filename>" (the typical `sha256sum` output format).
HASH_MANIFEST_NAMES = ("SHA256SUMS", "SHA256SUMS.txt", "sha256sums.txt", "checklist.chk")


def _sha256_of_file(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _collect_expected_hashes(directory: Path) -> dict[str, str]:
    """Gather {filename: expected_sha256} from any hash manifest(s) and/or
    per-file `<weight>.sha256` sidecars present in the directory."""
    expected: dict[str, str] = {}
    for name in HASH_MANIFEST_NAMES:
        manifest = directory / name
        if not manifest.is_file():
            continue
        try:
            for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    expected[parts[-1].lstrip("*")] = parts[0].lower()
        except OSError:
            continue
    for sidecar in directory.glob("*.sha256"):
        target = sidecar.name[: -len(".sha256")]
        try:
            content = sidecar.read_text(encoding="utf-8", errors="ignore").split()
        except OSError:
            continue
        if content:
            expected[target] = content[0].strip().lower()
    return expected


def verify_model_hashes(directory: str | Path) -> dict:
    """Deep, opt-in SHA256 verification of a model's weight files.

    Reads every file that has an expected hash (in a SHA256SUMS-style
    manifest or a `<file>.sha256` sidecar) and recomputes its SHA256. This
    is EXPENSIVE (multi-GB reads) and is therefore never run during
    scan_models — call it explicitly (e.g. `cli model_check --verify-hash`).

    Returns {"available": bool, "ok": bool, "files": [{file, ok, detail}...]}.
    `available` is False when the model ships no hash manifest at all (so the
    caller can say "no hashes to check" rather than "passed").
    """
    directory = Path(directory)
    expected = _collect_expected_hashes(directory)
    if not expected:
        return {"available": False, "ok": True, "files": []}

    files: list[dict] = []
    all_ok = True
    for filename, exp_hash in sorted(expected.items()):
        target = directory / filename
        if not target.is_file():
            files.append({"file": filename, "ok": False, "detail": "清单中列出的文件不存在"})
            all_ok = False
            continue
        try:
            actual = _sha256_of_file(target)
        except OSError as exc:
            files.append({"file": filename, "ok": False, "detail": f"读取失败: {exc}"})
            all_ok = False
            continue
        ok = actual == exp_hash
        files.append({
            "file": filename, "ok": ok,
            "detail": "校验通过" if ok else f"哈希不匹配(期望 {exp_hash[:12]}…，实际 {actual[:12]}…)",
        })
        if not ok:
            all_ok = False
    return {"available": True, "ok": all_ok, "files": files}


def _build_model_info(directory: Path) -> ModelInfo:
    info = ModelInfo(name=directory.name, path=str(directory))

    try:
        info.format = _detect_format(directory)
        info.engine_hint = {
            "safetensors": "vllm", "gguf": "vllm",
        }.get(info.format, "unknown")
        info.size_bytes = _total_size(directory, (SAFETENSORS_SUFFIX, GGUF_SUFFIX))

        config = _parse_config_json(directory, info)
        if not config and info.format == "safetensors":
            info.warnings.append("缺少或无法解析 config.json")

        info.is_embedding = _looks_like_embedding(directory, config)
        info.quantization = _detect_quantization(directory, config)
        # torch_dtype can live at the top level or (for multimodal configs
        # like Qwen3-VL) nested under text_config — check both.
        dtype = config.get("torch_dtype") or config.get("dtype")
        if dtype is None:
            text_cfg = config.get("text_config")
            if isinstance(text_cfg, dict):
                dtype = text_cfg.get("torch_dtype") or text_cfg.get("dtype")
        if isinstance(dtype, str):
            info.torch_dtype = dtype
        info.tool_call_capable = _detect_tool_call_capable(directory)
        info.multimodal, info.modalities = _detect_multimodal(directory, config)
        info.is_moe, info.num_experts, info.num_experts_per_tok = _detect_moe(config)
        if info.format == "safetensors":
            info.param_count = _detect_param_count(directory, config)
        hidden = _cfg_value(config, "hidden_size")
        layers = _cfg_value(config, "num_hidden_layers")
        info.hidden_size = hidden if isinstance(hidden, int) else None
        info.num_hidden_layers = layers if isinstance(layers, int) else None
        if info.format == "gguf":
            _detect_gguf_vllm_compat(directory, info)

        max_pos = config.get("max_position_embeddings")
        if max_pos is None:
            text_cfg = config.get("text_config")
            if isinstance(text_cfg, dict):
                max_pos = text_cfg.get("max_position_embeddings")
        if isinstance(max_pos, int):
            info.max_position_embeddings = max_pos

        architectures = config.get("architectures")
        if isinstance(architectures, list):
            info.architectures = [str(a) for a in architectures]

        model_type = config.get("model_type")
        if model_type is None:
            text_cfg = config.get("text_config")
            if isinstance(text_cfg, dict):
                model_type = text_cfg.get("model_type")
        if isinstance(model_type, str):
            info.model_type = model_type

        # Tokenizer artifacts (recorded for both formats; GGUF embeds its own
        # tokenizer so absence of separate files is fine there).
        info.tokenizer_present, info.tokenizer_files = _detect_tokenizer(directory)

        _validate_model(directory, info, config)

    except Exception as exc:  # pragma: no cover - never abort the whole scan
        info.warnings.append(f"解析模型目录时发生未预期错误: {exc}")
        info.valid = False
        info.validation_errors.append(f"扫描时发生异常: {exc}")
        logger.warning("模型目录解析异常 %s: %s", directory, exc)

    return info


def scan_models(root_dir: str | Path) -> list[ModelInfo]:
    """Recursively scan root_dir and return a ModelInfo per model directory.

    A directory is a "model" if it directly contains config.json or any
    .gguf file. Scanning continues past any single directory's failure.
    """
    root = Path(root_dir)
    if not root.exists():
        logger.warning("模型根目录不存在: %s", root)
        return []

    models: list[ModelInfo] = []
    for directory in sorted(p for p in root.rglob("*") if p.is_dir()):
        try:
            if _is_model_dir(directory):
                models.append(_build_model_info(directory))
        except OSError as exc:
            logger.warning("无法访问目录 %s: %s", directory, exc)
            continue

    return models


def list_general_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Filter out embedding/reranker models, returning general chat/instruct models."""
    return [m for m in models if not m.is_embedding]


def list_embedding_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Return only embedding/reranker models."""
    return [m for m in models if m.is_embedding]


# ---------------------------------------------------------------------------
# Persisted scan result (so scanning is a manual/explicit action, not a
# per-request automatic cost). The result file is the shared source of truth
# read by the CLI, the Web backend cache, and any other caller.
# ---------------------------------------------------------------------------

SCAN_RESULT_FILENAME = "model_scan_result.json"


def scan_result_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / SCAN_RESULT_FILENAME


def save_scan_result(models: list[ModelInfo], data_dir: str | Path, model_root_dir: str | Path) -> dict:
    """Persist a scan result to data_dir/model_scan_result.json and return the
    payload. Splits into general/embedding (backward-compatible with the
    format cli model_check has always written) and stamps scanned_at."""
    path = scan_result_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scanned_at": time.time(),
        "model_root_dir": str(model_root_dir),
        "general_models": [m.to_dict() for m in list_general_models(models)],
        "embedding_models": [m.to_dict() for m in list_embedding_models(models)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_scan_result(data_dir: str | Path) -> dict | None:
    """Load a persisted scan result. Returns {scanned_at, model_root_dir,
    models:[ModelInfo]} or None if no (readable) result file exists."""
    path = scan_result_path(data_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取扫描结果失败 %s: %s", path, exc)
        return None
    raw = list(data.get("general_models", [])) + list(data.get("embedding_models", []))
    models = [ModelInfo.from_dict(d) for d in raw if isinstance(d, dict)]
    return {
        "scanned_at": data.get("scanned_at"),
        "model_root_dir": data.get("model_root_dir"),
        "models": models,
    }
