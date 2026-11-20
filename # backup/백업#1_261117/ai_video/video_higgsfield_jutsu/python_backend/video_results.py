"""Poll existing provider jobs, review outputs and publish completed videos."""

import asyncio
import json
import logging
import time

from fastapi import HTTPException
from .video_errors import AssetError, VideoError

from .video_validation import validate_video_result
from .ai_usage import TERMINAL_VIDEO_STATES, report_usage, usage_row
from .work_settings import video_analysis_model


TERMINAL = ("completed", "failed")


class LostLease(Exception):
    pass

async def _review_completed_video(self, job, owner, reference, candidate, folder, update):
    job_id = job["id"]
    options = job["options"]
    api_key = self.service.config.get("OPENAI_API_KEY")
    review_model = video_analysis_model(self.service, options)
    optimize_request = self._analysis_request
    update(message="생성 영상의 외형·배치·카메라·동작을 표본 검증합니다.")
    current = self.store.get(job_id)
    output_images = []
    missing_assets = []
    for asset in options.get("assets", []):
        try:
            asset_info, asset_path = self.assets.get(asset["id"])
            output_images.append({**asset_info, "path": asset_path})
        except (AssetError, FileNotFoundError, OSError):
            missing_assets.append(asset["id"])
    reference_path = reference if reference.is_file() else None
    if reference_path is None:
        try:
            stored_source = self.source(current["source"])
            if "path" in stored_source:
                reference_path = stored_source["path"]
        except (HTTPException, VideoError, OSError):
            pass
    transcript = current["metadata"].get("previsTranscript") or {}
    source_duration = transcript.get("sourceDuration") or options["duration"]
    critical_moments = [
        {**moment, "time": moment["time"] * options["duration"] / source_duration}
        for moment in transcript.get("plan", {}).get("criticalMoments", [])
    ]
    output_report = await validate_video_result(
        optimize_request if api_key else None,
        model=review_model,
        result=candidate,
        reference=reference_path,
        images=output_images,
        directory=folder / "validation" / owner,
        user_prompt=options["prompt"],
        scene_prompt=options.get("scenePrompt", ""),
        submitted_prompt=current["metadata"].get("submittedPrompt", ""),
        cached=current["metadata"].get("outputValidation"),
        critical_moments=critical_moments,
        character_direction={key: current["metadata"]["characterDirection"][key]
                             for key in ("status", "cast", "plan")}
                            if current["metadata"].get("characterDirection") else None,
    )
    if missing_assets:
        output_report["missingAssetIds"] = missing_assets
        if output_report["status"] == "passed":
            output_report["status"] = "incomplete"
    update(metadata={**current["metadata"], "outputValidation": output_report})
    logging.getLogger("uvicorn.error").info(
        "[video-validation] job=%s output=%s findings=%s",
        job_id,
        output_report["status"],
        json.dumps(output_report["checks"], ensure_ascii=False),
    )


async def _wait_for_result(self, job, owner, reference, candidate, folder, update):
    job_id = job["id"]
    options = job["options"]
    client = self.adapter_for(options)
    deadline = time.monotonic() + 1200

    def save_usage(row):
        metadata = self.store.get(job_id)["metadata"]
        if not metadata.get("ai_usage"):
            update(metadata={**metadata, "ai_usage": [row]})

    while True:
        result = await client.poll(job["provider_id"], options["region"])
        state = result.get("task_status")
        if state in TERMINAL_VIDEO_STATES and client.capabilities.provider == "wan":
            report_usage(save_usage, usage_row(
                "dashscope", client.capabilities.model, {"usage": result.get("_usage")},
                job_id=job_id, provider_request_id=result.get("_request_id"),
                status=state.lower(), error_code=result.get("code"),
            ))
        if state == "SUCCEEDED":
            update(state="downloading", message="완성된 영상을 저장합니다.")
            url = result.get("video_url")
            if not isinstance(url, str):
                raise VideoError("영상 결과 영상 주소가 비어 있습니다.")
            await client.download(url, candidate, max_bytes=500 * 1024 * 1024)
            await self._review_completed_video(job, owner, reference, candidate, folder, update)
            if not self.store.publish_result(job_id, owner, candidate, folder / "wan.mp4"):
                raise LostLease()
            logging.getLogger("uvicorn.error").info("[작업 %s] 영상 생성 완료", job_id)
            self.result_asset(self.store.get(job_id))
            return
        if state in ("FAILED", "CANCELED", "UNKNOWN"):
            message = client.failure_message(result)
            metadata = {**self.store.get(job_id)["metadata"], "providerError": message}
            update(state="failed", message=message, metadata=metadata, lease=0)
            return
        if state not in ("PENDING", "RUNNING") or time.monotonic() > deadline:
            raise VideoError(
                "영상 작업 상태를 확정하지 못했습니다. 작업을 재생성하지 않고 기존 ID로 상태 조회를 이어갑니다."
            )
        await asyncio.sleep(15)


