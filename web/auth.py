"""Username+password login and JWT issuance/verification.

The secret key is read from core.config.AppConfig (auto-generated on first
run, see core/config.py). Tokens are short-lived bearer JWTs; there is no
refresh-token flow in this phase — re-login when the token expires.

Security note: login failure always returns the same 401 message whether
the username exists or not, to avoid leaking account enumeration info.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from core.config import AppConfig, load_config

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_SECONDS = 8 * 60 * 60  # 8 hours

# Generic message — never reveal whether the username exists.
INVALID_CREDENTIALS_MESSAGE = "用户名或密码错误"

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def get_config() -> AppConfig:
    """Loaded once per call; cheap (small YAML file), keeps state fresh
    if settings are updated via /api/settings without restarting."""
    return load_config()


def authenticate(username: str, password: str, config: AppConfig) -> bool:
    """Constant-shape comparison against the configured admin credentials.

    Returns True only if both username and password match exactly.
    """
    return username == config.admin_username and password == config.admin_password


def create_access_token(username: str, config: AppConfig,
                         expires_in_seconds: int = ACCESS_TOKEN_EXPIRE_SECONDS) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=expires_in_seconds),
    }
    return jwt.encode(payload, config.secret_key, algorithm=ALGORITHM)


def verify_token(token: str, config: AppConfig) -> dict:
    """Decode and validate a JWT, raising HTTPException(401) on failure."""
    try:
        payload = jwt.decode(token, config.secret_key, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="无效的身份凭证") from exc


def get_current_user(token: str | None = Depends(_oauth2_scheme)) -> str:
    """FastAPI dependency: protects a route, returns the authenticated username."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="缺少身份凭证", headers={"WWW-Authenticate": "Bearer"})
    config = get_config()
    payload = verify_token(token, config)
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="无效的身份凭证")
    return username


def verify_token_for_ws(token: str | None) -> str | None:
    """Best-effort token check for WebSocket query-param auth.

    Returns the username on success, or None on any failure (caller decides
    whether to close the connection).
    """
    if not token:
        return None
    try:
        config = get_config()
        payload = verify_token(token, config)
        return payload.get("sub")
    except HTTPException:
        return None
