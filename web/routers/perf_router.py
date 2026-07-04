"""POST /api/perf/run — lightweight concurrent throughput probe against a
loaded model's OpenAI-compatible endpoint, plus report listing/retrieval.

Scope note (see also web/schemas.py PerfRunRequest docstring and
研发方案.md 阶段五): this is a project-defined, self-contained benchmark —
it fires N requests at `concurrency` parallelism against
/v1/chat/completions, measures wall-clock latency and (optionally, when
stream=True) time-to-first-token, and reports aggregate throughput in
completion tokens/sec. It deliberately does NOT attempt to integrate or
claim conformance with a standardized suite such as MLPerf Inference or
LLMPerf — that is a substantially larger effort (different prompt/dataset
corpora, accuracy scoring, official harness dependencies) explicitly
deferred to a later phase if needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from core.config import load_config
from web.auth import get_current_user
from web.schemas import (
    PerfReportListItem,
    PerfReportListResponse,
    PerfReportOut,
    PerfRequestResult,
    PerfRunRequest,
    PerfRunSummary,
)
from web.state import registry
from web.ws_hub import manager as ws_manager

# How much of an upstream error response body to keep in the report — enough
# to see a vllm/JSON error payload, capped so a runaway HTML error page can't
# bloat the saved report.
_RESPONSE_EXCERPT_LIMIT = 2000

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/perf", tags=["perf"], dependencies=[Depends(get_current_user)])

REQUEST_TIMEOUT_SECONDS = 300.0


def _reports_dir(data_dir: str) -> Path:
    path = Path(data_dir) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _request_excerpt(url: str, headers: dict, body: dict) -> dict:
    return {"method": "POST", "url": url, "headers": dict(headers), "body": body}


async def _run_one_request(client: httpx.AsyncClient, url: str, body: dict, index: int,
                            stream: bool, req_headers: dict) -> PerfRequestResult:
    start = time.monotonic()
    ttft_ms: float | None = None
    try:
        if stream:
            body = {**body, "stream": True}
            async with client.stream("POST", url, json=body) as response:
                status_code = response.status_code
                raw = b""
                async for chunk in response.aiter_bytes():
                    if chunk and ttft_ms is None:
                        ttft_ms = round((time.monotonic() - start) * 1000, 1)
                    if status_code >= 300 and len(raw) < _RESPONSE_EXCERPT_LIMIT:
                        raw += chunk  # only bother buffering the body on error
                total_ms = round((time.monotonic() - start) * 1000, 1)
                ok = 200 <= status_code < 300
                return PerfRequestResult(
                    index=index, ok=ok, status_code=status_code,
                    ttft_ms=ttft_ms, total_ms=total_ms,
                    prompt_tokens=None, completion_tokens=None,
                    error=None if ok else f"HTTP {status_code}",
                    request_excerpt=None if ok else _request_excerpt(url, req_headers, body),
                    response_excerpt=None if ok else raw.decode("utf-8", "replace")[:_RESPONSE_EXCERPT_LIMIT],
                )

        response = await client.post(url, json=body)
        total_ms = round((time.monotonic() - start) * 1000, 1)
        ok = 200 <= response.status_code < 300
        prompt_tokens = completion_tokens = None
        if ok:
            try:
                payload = response.json()
                usage = payload.get("usage") or {}
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
            except ValueError:
                pass
        return PerfRequestResult(
            index=index, ok=ok, status_code=response.status_code,
            ttft_ms=None, total_ms=total_ms,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            error=None if ok else f"HTTP {response.status_code}",
            request_excerpt=None if ok else _request_excerpt(url, req_headers, body),
            response_excerpt=None if ok else response.text[:_RESPONSE_EXCERPT_LIMIT],
        )
    except httpx.HTTPError as exc:
        total_ms = round((time.monotonic() - start) * 1000, 1)
        # Network-level failure (timeout, connection refused, ...): no HTTP
        # response to capture, but record the request so the user sees what
        # was attempted and the exception type.
        return PerfRequestResult(
            index=index, ok=False, total_ms=total_ms,
            error=f"{type(exc).__name__}: {exc}",
            request_excerpt=_request_excerpt(url, req_headers, body),
            response_excerpt=None,
        )


@router.post("/run", response_model=PerfReportOut)
async def run_perf_test(payload: PerfRunRequest) -> PerfReportOut:
    loaded = registry.get(payload.model_name)
    if loaded is None or not loaded.host_port:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail=f"模型 {payload.model_name} 当前未加载或未记录监听端口")

    config = load_config()
    url = f"http://127.0.0.1:{loaded.host_port}/v1/chat/completions"
    body_template = {
        "model": payload.model_name,
        "messages": [{"role": "user", "content": payload.prompt}],
        "max_tokens": payload.max_tokens,
        "temperature": payload.temperature,
    }
    # Every vllm container runs with --api-key, so the perf probe must
    # authenticate too — without this header every request comes back HTTP
    # 401 and the whole test "fails" (the symptom the user reported). Same
    # fix as the debug forwarder; vllm only accepts Bearer.
    headers = {"Content-Type": "application/json"}
    if config.vllm_api_key:
        headers["Authorization"] = f"Bearer {config.vllm_api_key}"

    report_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    total = payload.num_requests

    semaphore = asyncio.Semaphore(max(1, payload.concurrency))
    results: list[PerfRequestResult] = [None] * total  # type: ignore[list-item]
    completed = 0

    async def _broadcast_progress(stage: str) -> None:
        # Live progress for the 性能测试 page over WS (topic: perf_progress).
        # Never let a broadcast hiccup abort the test itself.
        try:
            await ws_manager.broadcast("perf_progress", {
                "report_id": report_id, "model_name": payload.model_name,
                "completed": completed, "total": total,
                "failed": sum(1 for r in results if r is not None and not r.ok),
                "stage": stage,
            })
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("perf 进度广播失败: %s", exc)

    async def _bounded(idx: int, client: httpx.AsyncClient) -> None:
        nonlocal completed
        async with semaphore:
            results[idx] = await _run_one_request(client, url, body_template, idx, payload.stream, headers)
        completed += 1  # asyncio is single-threaded → no lock needed
        await _broadcast_progress("running")

    overall_start = time.monotonic()
    await _broadcast_progress("start")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
        await asyncio.gather(*[_bounded(i, client) for i in range(total)])
    total_duration_ms = round((time.monotonic() - overall_start) * 1000, 1)
    await _broadcast_progress("done")

    successful = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    total_completion_tokens = sum(r.completion_tokens or 0 for r in successful)
    avg_total_ms = round(sum(r.total_ms or 0 for r in successful) / len(successful), 1) if successful else None
    ttft_values = [r.ttft_ms for r in successful if r.ttft_ms is not None]
    avg_ttft_ms = round(sum(ttft_values) / len(ttft_values), 1) if ttft_values else None
    throughput = (
        round(total_completion_tokens / (total_duration_ms / 1000.0), 2)
        if total_duration_ms > 0 and total_completion_tokens > 0 else None
    )

    summary = PerfRunSummary(
        total_requests=payload.num_requests,
        successful=len(successful),
        failed=len(failed),
        total_duration_ms=total_duration_ms,
        avg_total_ms=avg_total_ms,
        avg_ttft_ms=avg_ttft_ms,
        total_completion_tokens=total_completion_tokens,
        throughput_tokens_per_sec=throughput,
    )

    report = PerfReportOut(
        report_id=report_id,
        created_at=time.time(),
        model_name=payload.model_name,
        concurrency=payload.concurrency,
        num_requests=payload.num_requests,
        max_tokens=payload.max_tokens,
        summary=summary,
        results=results,
    )

    report_path = _reports_dir(config.data_dir) / f"perf_{report_id}.json"
    try:
        report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("写入性能测试报告失败 %s: %s", report_path, exc)

    return report


@router.get("/reports", response_model=PerfReportListResponse)
def list_perf_reports() -> PerfReportListResponse:
    config = load_config()
    reports_dir = _reports_dir(config.data_dir)
    items: list[PerfReportListItem] = []
    for path in sorted(reports_dir.glob("perf_*.json"), reverse=True):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items.append(PerfReportListItem(
                report_id=raw["report_id"], created_at=raw["created_at"],
                model_name=raw["model_name"], concurrency=raw["concurrency"],
                num_requests=raw["num_requests"], summary=PerfRunSummary(**raw["summary"]),
            ))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("解析性能测试报告失败 %s: %s", path, exc)
            continue
    return PerfReportListResponse(reports=items)


def _report_path_for(data_dir: str, report_id: str) -> Path:
    """Resolve a report_id to its file path, rejecting any id that would
    escape the reports directory (path-traversal guard, since report_id
    comes from the URL)."""
    reports_dir = _reports_dir(data_dir)
    path = (reports_dir / f"perf_{report_id}.json").resolve()
    if path.parent != reports_dir.resolve():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法的报告ID")
    return path


@router.delete("/reports")
def clear_perf_reports() -> dict:
    """Delete ALL performance test history reports."""
    config = load_config()
    reports_dir = _reports_dir(config.data_dir)
    deleted = 0
    for path in reports_dir.glob("perf_*.json"):
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("删除性能报告失败 %s: %s", path, exc)
    return {"ok": True, "deleted": deleted, "message": f"已删除 {deleted} 条历史记录"}


@router.delete("/reports/{report_id}")
def delete_perf_report(report_id: str) -> dict:
    """Delete a single performance test report by id."""
    config = load_config()
    report_path = _report_path_for(config.data_dir, report_id)
    if not report_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到报告: {report_id}")
    try:
        report_path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                             detail=f"删除报告失败: {exc}") from exc
    return {"ok": True, "report_id": report_id, "message": "已删除该测试记录"}


@router.get("/reports/{report_id}", response_model=PerfReportOut)
def get_perf_report(report_id: str) -> PerfReportOut:
    config = load_config()
    report_path = _reports_dir(config.data_dir) / f"perf_{report_id}.json"
    if not report_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到报告: {report_id}")
    try:
        raw = json.loads(report_path.read_text(encoding="utf-8"))
        return PerfReportOut(**raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                             detail=f"读取报告失败: {exc}") from exc
