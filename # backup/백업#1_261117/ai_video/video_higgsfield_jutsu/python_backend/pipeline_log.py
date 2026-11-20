"""Short server logs for generation requests and jobs."""

from functools import wraps
import logging
import time
from uuid import uuid4

from fastapi import HTTPException

from .store import redact


logger = logging.getLogger("uvicorn.error")


def log_message(value):
    """Keep error details on one line and mask known sensitive forms."""
    return " ".join(redact(str(value)).split())[:300]


def log_request(label):
    def decorate(handler):
        @wraps(handler)
        async def wrapped(request):
            request_id = uuid4().hex
            request.state.pipeline_id = request_id
            started = time.monotonic()
            logger.info("[요청 %s] %s 요청 접수", request_id, label)
            try:
                return await handler(request)
            except HTTPException as error:
                log = logger.error if error.status_code >= 500 else logger.warning
                log("[요청 %s] %s 요청 실패 · HTTP %s · %s · %.2f초",
                    request_id, label, error.status_code, log_message(error.detail),
                    time.monotonic() - started)
                raise
            except Exception as error:
                logger.error("[요청 %s] %s 요청 처리 중 오류 · %s · %.2f초",
                             request_id, label, type(error).__name__, time.monotonic() - started)
                raise
        return wrapped
    return decorate
