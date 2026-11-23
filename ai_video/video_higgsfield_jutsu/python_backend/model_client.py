"""OpenAI request retry, error normalization and usage recording."""

import asyncio
import re
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from ai_video.model_gateway import routed_post
from .ai_usage import report_usage, usage_row
from .prompt_filter import guard_model_request




async def request_openai(http, api_key, path, body, emit, *, stage_label=None, sleep=asyncio.sleep,
                         usage_sink=None, job_id=None, session_id=None, allow_fallback=True):
    body = guard_model_request(path, body)
    started = time.monotonic()
    route_metadata = {'provider':'openai', 'model':body.get('model')}
    fallback_used = False
    label = stage_label or ("입력 검토" if path == "moderations" else "메인 워커 통합")
    permanent_limits = {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }

    def capture(data, status, attempt_started, request_id=None, error_code=None):
        if path == "responses" and usage_sink is not None:
            report_usage(usage_sink, usage_row(
                route_metadata['provider'], route_metadata['model'], data, job_id=job_id, session_id=session_id,
                status=status, error_code=error_code, provider_request_id=request_id,
                latency_ms=round((time.monotonic() - attempt_started) * 1000),
            ))

    try:
        for attempt in range(3):
            delay = float(2**attempt)
            attempt_started = time.monotonic()
            try:
                response = await routed_post(http, api_key, path, body, allow_fallback=allow_fallback)
                route_metadata = response.extensions.get('model_route', route_metadata)
                if response.extensions.get('model_fallback'):
                    fallback_used = True
                    # Any subsequent retry stays on GPT; do not restart the failed provider.
                    body = {**body, 'model':route_metadata['model']}
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if response.is_success and isinstance(data, dict):
                    capture(data, data.get("status") or "completed", attempt_started,
                            response.headers.get("x-request-id"))
                    if fallback_used:
                        data = {**data, '_model_route':route_metadata}
                    return data
                error = data.get("error", {}) if isinstance(data, dict) else {}
                error = error if isinstance(error, dict) else {}
                code = error.get("code") or error.get("type") or "unknown_error"
                retry = response.status_code in (408, 429) or response.status_code >= 500
                if code in permanent_limits:
                    retry = False
                safe_code = (
                    code
                    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code)
                    else "unknown_error"
                )
                reason = f"HTTP {response.status_code}, {safe_code}"
                if response.is_success:
                    retry = True
                    reason = "INVALID_JSON_RESPONSE"
                capture(data, "failed", attempt_started, response.headers.get("x-request-id"),
                        "invalid_json_response" if response.is_success else safe_code)
                if not retry or attempt == 2:
                    raise RuntimeError(f"AI {label} 요청 실패 ({reason}).")
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        try:
                            wait = (
                                parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                            ).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            wait = delay
                    if math.isfinite(wait):
                        delay = max(delay, wait)
            except httpx.TransportError as error:
                fallback_route = getattr(error, 'model_fallback', None)
                if fallback_route:
                    fallback_used = True
                    route_metadata = fallback_route
                    body = {**body, 'model':fallback_route['model']}
                reason = type(error).__name__
                capture(None, "unknown", attempt_started, error_code=reason)
                if attempt == 2:
                    raise RuntimeError(
                        f"AI {label} 연결·응답 대기가 3회 실패했습니다 ({reason})."
                    ) from error
            emit(
                "log",
                f"[previs-recovery] AI {label} 일시 오류 ({reason}) · {delay:g}초 후 {attempt+2}/3차 재시도",
            )
            await sleep(delay)
    finally:
        emit("log", f"[previs-timing] {label}: {time.monotonic()-started:.2f}초")

