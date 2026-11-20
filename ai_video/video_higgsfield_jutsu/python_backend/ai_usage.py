"""Best-effort normalization of provider usage for job responses."""

from datetime import datetime, timezone
import json
import re
from uuid import NAMESPACE_URL, uuid4, uuid5


COLUMNS = (
    "id", "message_id", "chatbot_id", "session_id", "job_id", "request_id", "mb_id",
    "job_type", "provider", "model", "modality", "billing_unit", "input_tokens",
    "output_tokens", "cached_tokens", "total_tokens", "quantity", "duration_seconds",
    "credits_charged", "cost_usd", "call_status", "error_code", "latency_ms",
    "provider_request_id", "raw_usage", "created_at",
)
TERMINAL_VIDEO_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}


def usage_row(provider, model, response, *, job_id=None, session_id=None,
              provider_request_id=None, status=None, error_code=None, latency_ms=None):
    """Return a JSON-safe row, omitting unavailable columns."""
    try:
        return _usage_row(provider, model, response, job_id, session_id,
                          provider_request_id, status, error_code, latency_ms)
    except Exception:
        return None


def _usage_row(provider, model, response, job_id, session_id, request_id, status, error_code, latency_ms):
    response = response if isinstance(response, dict) else {}
    raw = response.get("usage")
    usage = raw if isinstance(raw, dict) else {}
    try:
        json.dumps(usage, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raw = None
    video = provider == "dashscope"
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    identifier = uuid5(NAMESPACE_URL, f"ai-usage:{provider}:{job_id}") if video and job_id else uuid4()
    row = {
        "id": str(identifier), "session_id": session_id, "job_id": job_id,
        "job_type": "video_generation" if video else "previs_chat",
        "provider": provider, "model": model, "modality": "video" if video else "text",
        "billing_unit": "second" if video else "token",
        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
        "cached_tokens": cached, "total_tokens": usage.get("total_tokens"),
        "quantity": usage.get("duration" if video else "total_tokens"),
        "duration_seconds": usage.get("output_video_duration") if video else None,
        "credits_charged": usage.get("credits_charged"), "cost_usd": usage.get("cost_usd"),
        "call_status": status, "error_code": error_code, "latency_ms": latency_ms,
        "provider_request_id": request_id or response.get("request_id"),
        "raw_usage": raw if isinstance(raw, dict) else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens", "quantity",
                "duration_seconds", "credits_charged", "cost_usd", "latency_ms"):
        value = row[key]
        if type(value) not in (int, float) or not 0 <= value < float("inf"):
            row[key] = None
    for key in ("error_code", "provider_request_id"):
        value = row[key]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
            row[key] = None
    return {key: row[key] for key in COLUMNS if row.get(key) is not None}


def report_usage(sink, row):
    if sink is not None and row is not None:
        try:
            sink({key: row[key] for key in COLUMNS if key in row})
        except Exception:
            pass
