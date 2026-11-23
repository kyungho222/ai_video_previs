"""Stable previs agent entry point; planning, execution and remote I/O have separate owners."""
import time
from . import previs_execution, previs_planning, scene_pipeline
from .remote_tools import (
    tool_kind,
    result_data,
    load_workflow_instructions,
    repairable_operation_failure,
    call_tool,
    settle_operation,
)
from .model_client import request_openai
from .previs_finalization import (
    SCENE_REVIEW_LIMIT,
    CAMERA_QUALITY_REPORT_CODE,
    camera_quality_feedback,
    capture_spatial_manifest,
    render_only_code,
    count_scene_review,
    finalize_current_previs,
)
from .previs_planning import PrevisPreparation


class JutsuAgent:
    def __init__(self, client, api_key, model="gpt-6.1-terra", transport=None):
        self.client = client
        self.api_key = api_key
        self.model = model
        self.transport = transport


    async def run(self, job, history, emit):
        return await scene_pipeline.run(self, job, history, emit)

    async def _connect(self, job, emit):
        started = time.monotonic()
        try:
            await self.client.start()
        finally:
            emit("log", f"[previs-timing] Higgsfield 연결 확인: {time.monotonic() - started:.2f}초")
        if self.client.status["state"] != "connected":
            raise RuntimeError(self.client.status["message"])
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다. .env를 확인하세요.")
        job["workflowInstructions"] = await load_workflow_instructions(self.client, emit)
        emit("checkpoint", None)


    async def _prepare_scene(self, request, job, history, emit):
        return await previs_planning._prepare_scene(self, request, job, history, emit)
