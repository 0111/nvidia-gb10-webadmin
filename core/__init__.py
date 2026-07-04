"""Core library shared by the CLI and the future Web backend.

Modules:
    config        - AppConfig load/save (YAML)
    env_doctor    - environment detection & self-healing checks
    model_scanner - local model directory scanner
    param_advisor - vllm launch parameter recommendation
    docker_helper - docker compose / docker CLI wrapper
"""

__version__ = "0.1.0"
