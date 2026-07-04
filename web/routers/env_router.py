"""GET /api/env/checklist, POST /api/env/fix/{check_name}.

Mirrors the CLI's `run_env_checklist` safety model: fixes are previewed
(confirmed=False) by default and only actually executed when the request
body explicitly sets confirmed=true.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from core import env_doctor
from web.auth import get_current_user
from web.schemas import CheckResultOut, EnvReportOut, FixRequest, FixResultOut

router = APIRouter(prefix="/api/env", tags=["env"], dependencies=[Depends(get_current_user)])

_FIX_FUNCS = {
    "cuda_compat": lambda confirmed, interface: env_doctor.fix_cuda_compat(confirmed=confirmed),
    "drop_caches": lambda confirmed, interface: env_doctor.fix_drop_caches(confirmed=confirmed),
    "swap": lambda confirmed, interface: env_doctor.fix_swap(confirmed=confirmed),
    "ethernet_speed": lambda confirmed, interface: env_doctor.fix_ethernet_speed(
        interface or "", confirmed=confirmed),
}


@router.get("/checklist", response_model=EnvReportOut)
def get_checklist() -> EnvReportOut:
    report = env_doctor.run_all_checks()
    return EnvReportOut(
        overall_status=report.overall_status,
        checks=[CheckResultOut.from_core(c) for c in report.checks],
    )


@router.post("/fix/{check_name}", response_model=FixResultOut)
def fix_check(check_name: str, payload: FixRequest) -> FixResultOut:
    fix_fn = _FIX_FUNCS.get(check_name)
    if fix_fn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail=f"未知的检查项: {check_name}")

    if not payload.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"该操作（{check_name}）需要确认：请在请求体中设置 confirmed=true 后重试",
        )

    if check_name == "ethernet_speed" and not payload.interface:
        # Resolve interface automatically from a fresh check if not provided.
        check = env_doctor.check_ethernet_speed()
        interface = check.details.get("interface")
        if not interface:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                 detail="无法确定网卡接口名，请在请求体中显式提供 interface")
        payload.interface = interface

    result = fix_fn(True, payload.interface)
    return FixResultOut.from_core(result)
