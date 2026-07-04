"""POST /api/auth/login — username+password login, issues a JWT."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from web.auth import ACCESS_TOKEN_EXPIRE_SECONDS, INVALID_CREDENTIALS_MESSAGE, authenticate, create_access_token, get_config
from web.schemas import LoginRequest, LoginResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    config = get_config()
    if not authenticate(payload.username, payload.password, config):
        # Same message regardless of whether the username exists.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail=INVALID_CREDENTIALS_MESSAGE)
    token = create_access_token(payload.username, config)
    return LoginResponse(access_token=token, expires_in_seconds=ACCESS_TOKEN_EXPIRE_SECONDS)
