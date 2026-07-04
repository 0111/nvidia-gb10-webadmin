"""FastAPI Web backend package.

This package is a thin HTTP/WebSocket shell around the `core` library
(env_doctor / model_scanner / param_advisor / docker_helper / config). It
does not re-implement any detection/scanning/recommendation/orchestration
logic — see ../research notes in 研发方案.md 阶段二 for the full design.
"""
