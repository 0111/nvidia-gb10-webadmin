"""docker compose / docker CLI wrapper.

Everything shells out via subprocess; no docker SDK dependency is used so
this stays lightweight on ARM64. None of this is wired to real deployment
templates yet (out of scope for this phase) — the interface is designed
to be complete so the Web backend can later call the exact same methods
the CLI uses.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from core.model_scanner import ModelInfo

# The default vllm image is a LOCAL-ONLY patched tag (built from
# deploy/vllm-patch.Dockerfile — see bootstrap.sh step [5/5]). It fixes the
# fastapi 0.137.1 / prometheus_fastapi_instrumentator 8.0.0 incompatibility in
# nvcr.io/nvidia/vllm:26.06-py3 that 500s every /v1/* inference call. Because
# it lives on no registry, a missing image makes `docker compose up` try to
# PULL it and fail opaquely — so ensure_vllm_image() self-heals by building it.
PATCHED_VLLM_IMAGE = "gb10-vllm:26.06-py3-patched"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def image_exists(image: str, timeout: float = 15.0) -> bool:
    """True if `image` is present in the local docker image store."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ensure_vllm_image(image: str, timeout: float = 1800.0) -> "CommandResult":
    """Make sure `image` exists locally before a model container is started.

    - If it already exists → no-op success (fast `docker image inspect`).
    - If it's missing AND equals PATCHED_VLLM_IMAGE → build it from
      deploy/vllm-patch.Dockerfile (idempotent; mirrors bootstrap.sh [5/5]).
      The first build may need to pull the 30GB base image, hence the generous
      default timeout.
    - If it's missing and is some other (non-patched) tag we don't know how to
      build → return a clear, actionable error instead of letting a later
      `docker compose up` fail with an opaque pull error.
    """
    inspect_cmd = f"docker image inspect {image}"
    if image_exists(image):
        return CommandResult(ok=True, command=inspect_cmd, stdout="镜像已存在", returncode=0)

    if image != PATCHED_VLLM_IMAGE:
        return CommandResult(
            ok=False, command=inspect_cmd,
            error=(f"镜像 {image} 在本机不存在，且不是本项目可自动构建的补丁镜像 "
                   f"({PATCHED_VLLM_IMAGE})。请先用 docker pull / docker build 准备好它再加载。"),
        )

    dockerfile = _project_root() / "deploy" / "vllm-patch.Dockerfile"
    context = _project_root() / "deploy"
    if not dockerfile.exists():
        return CommandResult(
            ok=False, command="docker build",
            error=f"找不到 {dockerfile}，无法自动构建补丁镜像 {image}",
        )

    cmd = ["docker", "build", "-f", str(dockerfile), "-t", image, str(context)]
    cmd_str = " ".join(cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return CommandResult(ok=False, command=cmd_str, error="docker 命令未找到")
    except subprocess.TimeoutExpired:
        return CommandResult(
            ok=False, command=cmd_str,
            error=(f"补丁镜像构建超时（>{int(timeout)}s）——首次构建可能正在拉取基础镜像 "
                   f"nvcr.io/nvidia/vllm:26.06-py3，请确认网络后重试"),
        )
    if result.returncode != 0:
        return CommandResult(
            ok=False, command=cmd_str, stdout=result.stdout, stderr=result.stderr,
            returncode=result.returncode,
            error="补丁镜像构建失败（确认能否拉取基础镜像 nvcr.io/nvidia/vllm:26.06-py3）",
        )
    return CommandResult(ok=True, command=cmd_str, stdout=result.stdout, stderr=result.stderr, returncode=0)


COMPOSE_TEMPLATE = """\
services:
  {service_name}:
    image: {image}
    container_name: {container_name}
    restart: "no"
    # Docker's default /dev/shm is 64MB. vLLM/PyTorch write large tensors to
    # shared memory when processing MULTIMODAL inputs (images/video) — 64MB is
    # exhausted immediately, surfacing as "No space left on device" the moment
    # a client uploads an image (plain text chat, which barely touches shm,
    # works fine). tmpfs only uses memory as it fills, so this is a cap, not a
    # reservation. 16g comfortably holds image tensors on the 128GB GB10.
    shm_size: "16gb"
    ports:
      - "{host_port}:{container_port}"
    environment:
      - LD_LIBRARY_PATH=/usr/local/cuda/compat:/usr/local/nvidia/lib:/usr/local/nvidia/lib64
    volumes:
      - "{model_root_host}:/models:ro"
      - "{cuda_compat_dir}:/usr/local/cuda/compat:ro"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    command:
{command_lines}
"""


def container_logs(container_name: str, tail: int = 200, timeout: float = 30.0) -> "CommandResult":
    """`docker logs --tail N <container_name>` directly, independent of any
    compose file or in-memory registry — works for any container this
    project ever creates (vllm general/embedding, SearXNG) regardless of
    whether the backend process that loaded it is the one currently
    running. Returns ok=False with a friendly error (not an exception) if
    the container doesn't exist or isn't running."""
    cmd = ["docker", "logs", "--tail", str(tail), container_name]
    cmd_str = " ".join(cmd)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode != 0:
            return CommandResult(
                ok=False, command=cmd_str, stdout=result.stdout, stderr=result.stderr,
                returncode=result.returncode,
                error="容器不存在或未创建" if "No such container" in result.stderr else None,
            )
        # docker logs interleaves stdout/stderr depending on the
        # container's own stream usage (vllm logs mostly to stderr) — show
        # both concatenated so nothing is silently dropped.
        return CommandResult(ok=True, command=cmd_str, stdout=result.stdout + result.stderr, returncode=0)
    except FileNotFoundError:
        return CommandResult(ok=False, command=cmd_str, error="docker 命令未找到")
    except subprocess.TimeoutExpired:
        return CommandResult(ok=False, command=cmd_str, error="命令执行超时")
    except Exception as exc:  # pragma: no cover - defensive
        return CommandResult(ok=False, command=cmd_str, error=str(exc))


@dataclass
class CommandResult:
    """Structured result of a shelled-out docker/docker-compose invocation."""

    ok: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None


@dataclass
class DockerComposeManager:
    """Thin wrapper around `docker compose` for a single compose file.

    Attributes:
        model_root_host: host path of the read-only model directory, used
            as the bind-mount source in rendered compose files.
        cuda_compat_dir: host path of the cuda-compat library directory.
    """

    model_root_host: str = "/home/spark/LocalModels/LLModels"
    cuda_compat_dir: str = "/usr/local/cuda-13.3/compat"

    def render_compose(
        self,
        model_info: ModelInfo,
        vllm_params: dict,
        image: str = "gb10-vllm:26.06-py3-patched",
        host_port: int = 8001,
        container_port: int = 8000,
        api_key: str | None = None,
    ) -> str:
        """Render a docker-compose.yml content string for the given model.

        vllm_params is expected to be the dict shape produced by
        `param_advisor.recommend_vllm_params` (each value has a "default"
        key); only non-None defaults are emitted as CLI flags.
        """
        service_name = "vllm-embedding" if model_info.is_embedding else "vllm-general"
        container_name = f"gb10-{service_name}"

        if model_info.format == "gguf":
            if model_info.gguf_multi_shard:
                raise ValueError(
                    f"模型 {model_info.name} 是多分片GGUF，vllm仅支持单文件GGUF，"
                    "请先用 tools/gguf_merge_shards.py 合并为单文件后再加载"
                )
            if model_info.gguf_vllm_compatible is False:
                raise ValueError(
                    f"模型 {model_info.name} 的GGUF量化类型不被vllm 0.22 GGUF加载器支持，无法加载"
                )
            gguf_files = sorted(Path(model_info.path).glob("*.gguf"))
            if not gguf_files:
                raise ValueError(f"模型目录中未找到 .gguf 文件: {model_info.path}")
            model_container_path = (
                f"/models/{gguf_files[0].relative_to(self.model_root_host).as_posix()}"
            )
        else:
            model_container_path = f"/models/{Path(model_info.path).relative_to(self.model_root_host).as_posix()}"

        # Each CLI flag and its value must be a *separate* argv token —
        # combining them ("--flag value") into one string breaks the
        # container entrypoint's exec call (it is not parsed by a shell).
        # The image's entrypoint does a bare `exec "$@"` with no default
        # binary, so the command list must start with the actual
        # executable ("vllm serve"), not just CLI flags.
        # Model path is passed as the POSITIONAL `model_tag` (right after
        # `serve`), NOT via --model: vllm 0.22 deprecates the --model option
        # for `vllm serve` ("provide the model as a positional argument … The
        # --model option will be removed in a future version") — confirmed by
        # the real container's startup warning.
        flags: list[str] = ["vllm", "serve", model_container_path, "--served-model-name", model_info.name]
        flag_map = {
            "gpu_memory_utilization": "--gpu-memory-utilization",
            "max_model_len": "--max-model-len",
            "max_num_seqs": "--max-num-seqs",
            "max_num_batched_tokens": "--max-num-batched-tokens",
            "kv_cache_dtype": "--kv-cache-dtype",
            "dtype": "--dtype",
            "quantization": "--quantization",
            "tokenizer_mode": "--tokenizer-mode",
            "tokenizer": "--tokenizer",
            "tool_call_parser": "--tool-call-parser",
            "convert": "--convert",
            "runner": "--runner",
        }
        for key, flag in flag_map.items():
            entry = vllm_params.get(key)
            if entry and entry.get("default") is not None:
                flags.append(flag)
                flags.append(str(entry["default"]))

        # vllm's GGUF quantization layer only dequantizes to float16/float32;
        # bfloat16 (vllm's "auto" dtype choice on most models, and the only
        # option vllm itself warns is imprecise for GGUF on Blackwell) raises
        # a pydantic ValidationError at startup — confirmed on the real
        # server. Force float16 for any GGUF model regardless of the
        # dtype param's value, unless the user explicitly chose float32.
        if model_info.format == "gguf":
            dtype_entry = vllm_params.get("dtype") or {}
            if dtype_entry.get("default") not in ("float16", "float32"):
                if "--dtype" in flags:
                    flags[flags.index("--dtype") + 1] = "float16"
                else:
                    flags += ["--dtype", "float16"]

        bool_flag_map = {
            "enable_auto_tool_choice": "--enable-auto-tool-choice",
            "enable_chunked_prefill": "--enable-chunked-prefill",
            "enable_prefix_caching": "--enable-prefix-caching",
            "trust_remote_code": "--trust-remote-code",
            "enforce_eager": "--enforce-eager",
        }
        for key, flag in bool_flag_map.items():
            entry = vllm_params.get(key)
            if entry and entry.get("default"):
                flags.append(flag)

        if api_key:
            flags += ["--api-key", api_key]

        command_lines = "\n".join(f'      - "{f}"' for f in flags)

        return COMPOSE_TEMPLATE.format(
            service_name=service_name,
            image=image,
            container_name=container_name,
            host_port=host_port,
            container_port=container_port,
            model_root_host=self.model_root_host,
            cuda_compat_dir=self.cuda_compat_dir,
            command_lines=command_lines,
        )

    def _run_compose(self, compose_path: str | Path, args: list[str], timeout: float = 60.0) -> CommandResult:
        cmd = ["docker", "compose", "-f", str(compose_path), *args]
        cmd_str = " ".join(cmd)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False,
            )
            return CommandResult(
                ok=result.returncode == 0, command=cmd_str,
                stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            )
        except FileNotFoundError:
            return CommandResult(ok=False, command=cmd_str, error="docker 命令未找到")
        except subprocess.TimeoutExpired:
            return CommandResult(ok=False, command=cmd_str, error="命令执行超时")
        except Exception as exc:  # pragma: no cover - defensive
            return CommandResult(ok=False, command=cmd_str, error=str(exc))

    def start(self, compose_path: str | Path) -> CommandResult:
        return self._run_compose(compose_path, ["up", "-d"], timeout=300)

    def stop(self, compose_path: str | Path) -> CommandResult:
        return self._run_compose(compose_path, ["stop"], timeout=60)

    def restart(self, compose_path: str | Path) -> CommandResult:
        return self._run_compose(compose_path, ["restart"], timeout=120)

    def status(self, compose_path: str | Path) -> CommandResult:
        return self._run_compose(compose_path, ["ps", "--format", "json"], timeout=30)

    def down(self, compose_path: str | Path) -> CommandResult:
        """Tear the stack down (used by the `clean` CLI command)."""
        return self._run_compose(compose_path, ["down"], timeout=120)

    def logs(self, compose_path: str | Path, service: str | None = None, tail: int = 200) -> CommandResult:
        args = ["logs", "--tail", str(tail)]
        if service:
            args.append(service)
        return self._run_compose(compose_path, args, timeout=30)


DEFAULT_SEARXNG_COMPOSE_PATH = (
    Path(__file__).resolve().parent.parent / "deploy" / "searxng-compose.yml"
)


@dataclass
class SearxngManager:
    """Thin wrapper around `docker compose` for the fixed SearXNG stack.

    Always targets `deploy/searxng-compose.yml` (see deploy/README.md);
    follows the same subprocess-based `docker compose` invocation pattern
    as DockerComposeManager, but with a fixed compose file so callers
    don't need to know its on-disk location.
    """

    compose_path: str | Path = DEFAULT_SEARXNG_COMPOSE_PATH

    def _run(self, args: list[str], timeout: float = 60.0) -> CommandResult:
        cmd = ["docker", "compose", "-f", str(self.compose_path), *args]
        cmd_str = " ".join(cmd)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False,
            )
            return CommandResult(
                ok=result.returncode == 0, command=cmd_str,
                stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            )
        except FileNotFoundError:
            return CommandResult(ok=False, command=cmd_str, error="docker 命令未找到")
        except subprocess.TimeoutExpired:
            return CommandResult(ok=False, command=cmd_str, error="命令执行超时")
        except Exception as exc:  # pragma: no cover - defensive
            return CommandResult(ok=False, command=cmd_str, error=str(exc))

    def start(self) -> CommandResult:
        return self._run(["up", "-d"], timeout=300)

    def stop(self) -> CommandResult:
        return self._run(["down"], timeout=120)

    def status(self) -> CommandResult:
        return self._run(["ps", "--format", "json"], timeout=30)

    def logs(self, tail: int = 200) -> CommandResult:
        return self._run(["logs", "--tail", str(tail)], timeout=30)
