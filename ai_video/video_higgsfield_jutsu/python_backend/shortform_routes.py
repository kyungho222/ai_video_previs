"""HTTP routes for generating shortform scenarios."""

import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from .pipeline_log import log_request, logger
from .prompt_filter import PromptFilterError
from .shortform_scenario import generate_scenario
from .scenario_admission import ScenarioAdmissionCache
from .usage_registration import begin_scenario_usage
from .work_settings import request_work_metadata, effective_chat_model


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
    admission_cache = ScenarioAdmissionCache()

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
        model = effective_chat_model(service, body.get("model", service.default_model))
        if not isinstance(model, str) or model not in service.chat_models:
            raise HTTPException(400, detail="허용된 채팅 모델을 선택하세요.")
        try:
            request_work_metadata(body)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        request_id = request.state.pipeline_id
        session_id = str(uuid4())
        scenario_job_id = str(uuid4())
        usage = await begin_scenario_usage(service, body, job_id=scenario_job_id,
                                           session_id=session_id, request_id=request_id)
        stage_state = 'failed'
        try:
            started = time.monotonic()
            try:
                async with service.slots:
                    result = await generate_scenario(
                        title=title.strip(), summary=summary.strip(), model=model,
                        api_key=service.config.get("OPENAI_API_KEY"), transport=service.transport,
                        request_id=request_id,
                        usage_sink=usage.capture,
                        admission_cache=admission_cache,
                    )
            except PromptFilterError as error:
                raise HTTPException(error.status_code, detail=str(error)) from None
            except RuntimeError as error:
                raise HTTPException(502, detail=str(error)) from None
            logger.info("[요청 %s] 시나리오 생성 완료 · 씬 %d개 · %.2f초",
                        request_id, len(result["scenes"]), time.monotonic() - started)
            result["sessionId"] = service.scenarios.save({
                "job_id": scenario_job_id,
                **usage.snapshot_fields,
                "title": title.strip(), "summary": summary.strip(), "scenes": result["scenes"],
                "settings": {"model": model, "db_name": body.get("db_name", ""),
                             "chatbotid": body.get("chatbotid", "")},
            }, session_id=session_id)
            result['job_id'] = scenario_job_id
            usage.annotate_result(result)
            stage_state = 'completed'
            return result
        finally:
            usage.finish(stage_state)

    @router.put("/scenario/{session_id}")
    async def update_scenario(session_id: str, request: Request):
        if len(await request.body()) > 32768:
            raise HTTPException(413, detail="시나리오가 너무 깁니다.")
        try:
            record = service.scenarios.get(session_id)
            body = await request.json()
        except FileNotFoundError:
            raise HTTPException(404, detail="저장된 시나리오가 없습니다.") from None
        except ValueError:
            raise HTTPException(400, detail="시나리오 요청을 확인하세요.") from None
        scenes = body.get("scenes") if isinstance(body, dict) else None
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 40 or not all(
            isinstance(scene, dict) and isinstance(scene.get("title"), str)
            and 1 <= len(scene["title"].strip()) <= 120
            and isinstance(scene.get("prompt"), str) and 1 <= len(scene["prompt"].strip()) <= 8000
            for scene in scenes
        ):
            raise HTTPException(400, detail="시나리오 씬 목록을 확인하세요.")
        record["scenes"] = [{"title": scene["title"].strip(), "prompt": scene["prompt"].strip()} for scene in scenes]
        service.scenarios.save(record, session_id)
        return {"sessionId": session_id}

    return router
