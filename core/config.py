"""Application configuration: load/save AppConfig from a YAML file.

The config file is the single source of truth for ports, secrets and
filesystem locations. It lives under the project's data directory so the
whole deployment (config + state) can be copied to migrate the install.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


@dataclass
class AppConfig:
    """Top-level application configuration.

    Attributes:
        web_port: HTTP port for the future FastAPI web backend.
        web_host: Bind address for the web backend.
        secret_key: Random secret used for session/JWT signing.
        admin_username: Web login username.
        admin_password: Web login password (plaintext in file per project
            scope; rotate via `cli config` in later phases).
        model_root_dir: Read-only root directory containing local model files.
        data_dir: Writable directory for state/cache/logs/reports.
        searxng_port: Local SearXNG instance port.
        searxng_url: Base URL for the SearXNG search API.
        vllm_image: Pinned NVIDIA vllm container image.
        cuda_compat_dir: Host path of the cuda-compat library directory.
        searxng_proxy_url: Optional HTTP/HTTPS proxy URL
            (e.g. "http://127.0.0.1:7890") used for every real
            core.searxng_client.search() call, not just the one-off
            "test proxy" probe. None/empty means direct connection.
        frontend_port: Port the built Vue3 frontend is served on (via
            `vite preview`) when the CLI manages it as a project component.
        vllm_api_key: API key passed to every vllm container via
            `--api-key`, used as the Bearer token clients must send to the
            OpenAI-compatible (/v1/chat/completions, /v1/embeddings) and
            Anthropic-compatible (/v1/messages) endpoints vllm 0.22
            natively exposes. Generated once in the "sk-xxxx" shape API
            clients expect, then reused for every model — this is a
            single-tenant internal tool, so per-model keys would add
            rotation complexity with no real benefit.
    """

    web_port: int = 8000
    web_host: str = "0.0.0.0"
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32))
    admin_username: str = "admin"
    admin_password: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    model_root_dir: str = "/home/spark/LocalModels/LLModels"
    data_dir: str = "/home/spark/nvidia-gb10-manager/data"
    searxng_port: int = 8080
    searxng_url: str = "http://127.0.0.1:8080"
    vllm_image: str = "gb10-vllm:26.06-py3-patched"
    cuda_compat_dir: str = "/usr/local/cuda-13.3/compat"
    searxng_proxy_url: str | None = None
    frontend_port: int = 4173
    vllm_api_key: str = field(default_factory=lambda: "sk-" + secrets.token_hex(24))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load AppConfig from a YAML file.

    If the file does not exist, a fresh AppConfig with generated secrets is
    created and immediately persisted, so repeated calls (across processes,
    requests, or CLI invocations) always see the same secret_key/password
    instead of a new random one each time.

    Same logic applies when the file DOES exist but is missing a field
    that has been added to AppConfig since that file was first written
    (e.g. vllm_api_key, added in v1.1.0, on top of an already-deployed
    settings.yaml): any dataclass field with a default_factory generates a
    *fresh random value on every single load_config() call* unless it's
    actually present in the YAML. Concretely this meant a freshly-rendered
    vllm compose file's --api-key and the very next request's "what's the
    current key" check could already disagree, with every request to a
    loaded model failing 401 "Unauthorized" — confirmed by reproducing it
    live. So any field absent from the on-disk file is written back
    immediately after first being defaulted, exactly like the
    file-doesn't-exist-at-all case above.
    """
    path = Path(path)
    if not path.exists():
        config = AppConfig()
        save_config(config, path)
        return config

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    known_fields = {f for f in AppConfig.__dataclass_fields__}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    config = AppConfig(**filtered)

    missing_fields = known_fields - raw.keys()
    if missing_fields:
        save_config(config, path)

    return config


def save_config(config: AppConfig, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist AppConfig to a YAML file, creating parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config.to_dict(), fh, allow_unicode=True, sort_keys=False)
