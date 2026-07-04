"""GET/PUT /api/settings — advanced settings: ports, secrets, paths.

PUT performs a partial update: only fields present (non-None) in the
request body are changed; everything else is preserved from the current
on-disk config. secret_key / admin_password are returned in plaintext
(internal single-admin tool, no multi-tenant secrecy boundary to protect).
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends

from core.config import load_config, save_config
from web.auth import get_current_user
from web.schemas import SettingsOut, SettingsUpdateRequest

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(get_current_user)])


def _to_out(config) -> SettingsOut:
    return SettingsOut(
        web_port=config.web_port,
        web_host=config.web_host,
        admin_username=config.admin_username,
        model_root_dir=config.model_root_dir,
        data_dir=config.data_dir,
        searxng_port=config.searxng_port,
        searxng_url=config.searxng_url,
        vllm_image=config.vllm_image,
        cuda_compat_dir=config.cuda_compat_dir,
        searxng_proxy_url=config.searxng_proxy_url,
        frontend_port=config.frontend_port,
        secret_key=config.secret_key,
        admin_password=config.admin_password,
        vllm_api_key=config.vllm_api_key,
    )


@router.get("", response_model=SettingsOut)
def get_settings() -> SettingsOut:
    return _to_out(load_config())


@router.put("", response_model=SettingsOut)
def update_settings(payload: SettingsUpdateRequest) -> SettingsOut:
    config = load_config()

    updates = payload.model_dump(exclude_unset=True, exclude={"rotate_secret_key"})
    for field, value in updates.items():
        # searxng_proxy_url is the one field where "explicitly set to
        # null" is a meaningful, supported action (clear the proxy, go
        # back to a direct connection) rather than "field omitted" — every
        # other field skips None so an omitted/blank value never
        # accidentally wipes existing config (e.g. admin_password).
        if value is not None or field == "searxng_proxy_url":
            setattr(config, field, value)

    if payload.rotate_secret_key:
        config.secret_key = secrets.token_hex(32)

    save_config(config)
    return _to_out(config)
