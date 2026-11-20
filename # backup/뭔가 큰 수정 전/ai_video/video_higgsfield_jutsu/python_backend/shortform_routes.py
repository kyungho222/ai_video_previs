"""HTTP routes for generating shortform scenarios."""

import time

from fastapi import APIRouter, HTTPException, Request

from .pipeline_log import log_request, logger
from .prompt_filter import PromptFilterError
from .shortform_scenario import generate_scenario
from .work_settings import request_work_metadata


SCENARIO_REQUEST = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["title", "summary"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "model": {"type": "string", "description": "Defaults to the configured chat model."},
                    "db_name": {"type": "string"},
                    "chatbotid": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                },
            },
        },
    },
}


def create_shortform_router(service):
    router = APIRouter(prefix="/api/shortform", tags=["shortform"])

    @router.post("/scenario", openapi_extra={"requestBody": SCENARIO_REQUEST})
    @log_request("시나리오 생성")
    async def shortform_scenario(request: Request):
        if len(await request.body()) > 16384:
            raise HTTPException(413, detail="입력이 너무 깁니다.")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail="JSON 요청 형식이 올바르지 않습니다.") from None
        if not isinstance(body, dict):
            raise HTTPException(400, detail="제목과 내용을 입력하세요.")
        title, summary = body.get("title"), body.get("summary")
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 120
                or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 4000):
            raise HTTPException(400, detail="제목은 1~120자, 내용은 1~4000자로 입력하세요.")
        model = body.get("model", service.default_model)
        if not isinstance(model, str) or model not in service.chat_models:
            raise HTTPException(400, detail="허용된 채팅 모델을 선택하세요.")
        try:
            request_work_metadata(body)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        request_id = request.state.pipeline_id
        started = time.monotonic()
        try:
            async with service.slots:
                result = await generate_scenario(
                    title=title.strip(), summary=summary.strip(), model=model,
                    api_key=service.config.get("OPENAI_API_KEY"), transport=service.transport,
                    request_id=request_id,
                )
        except PromptFilterError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        except RuntimeError as error:
            raise HTTPException(502, detail=str(error)) from None
        logger.info("[요청 %s] 시나리오 생성 완료 · 씬 %d개 · %.2f초",
                    request_id, len(result["scenes"]), time.monotonic() - started)
        return result

    return router
