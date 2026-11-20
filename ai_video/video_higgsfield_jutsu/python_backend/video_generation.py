"""Shared video inputs, guide correction, task lifecycle and provider-neutral API."""

import asyncio
import contextlib
import json
import logging
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

import httpx
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    File,
    Form,
    UploadFile,
)
from fastapi.responses import FileResponse
from .agent import result_data, tool_kind, request_openai
from .media_archive import MediaExpired, ArchiveError
from .retention_policy import is_expired
from .pipeline_log import log_message, logger
from .spatial_guides import (
    GuideError,
    SpatialGuideBuilder,
    OpenAIGuideImageEditor,
    spatial_prompt_context,
    scene_text_without_asset_indices,
    bind_spatial_prompt,
    spatial_guide_budget,
    prepare_asset_parts,
)
from .video_errors import (
    MAX_REFERENCE_BYTES,
    MAX_IMAGE_BYTES,
    AssetError,
    VideoError,
    PromptLengthError,
)
from .video_media import (
    LimitedUploadRoute,
    UploadedPrevis,
    VideoAssets,
    PublicHTTPTransport,
    public_https,
    inspect_video,
    download_video,
    create_previs_upload_router,
)
from .job_repository import VideoJobs
from .video_prompt import (
    build_reference_prompt,
    timeline_duration,
    DEFAULT_VIDEO_PROMPT,
    PROMPT_POLICY_VERSION,
    PREVIS_RULES,
    CAMERA_GUIDE_RULES,
    optimize_video_prompt,
    reference_prompt_overhead,
    fit_video_prompt,
)

from .camera_reference import source_scene_reference, scene_project_id, prepare_camera_reference
from .video_models import video_adapters, validate_video_input
from .video_validation import validate_video_request, validate_video_result
from .previs_timeline import (
    describe_previs,
    endpoint_guides,
    timeline_prompt,
    preserve_timeline,
    bind_source_identity,
    compact_timeline,
)
from .character_direction import (
    character_records,
    prepare_character_direction,
    direction_prompt,
    preserve_character_direction,
)
from .ai_usage import TERMINAL_VIDEO_STATES, report_usage, usage_row
from .prompt_filter import (
    check_local_terms,
    filter_user_prompts,
    PromptFilterError,
    apply_presentation_rules,
    attach_presentation_rules,
)
from .repositories import local_repositories, runtime_base




UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
TERMINAL = ("completed", "failed")




from . import video_preparation, video_results
from .video_preparation import verified_guide_inputs
from .video_results import LostLease
from .video_routes import create_video_router


class VideoManager:
    def __init__(self, service):
        self.service = service
        self.root = runtime_base(service.root)
        repositories = getattr(service, "repositories", None)
        if repositories is None:
            repositories = local_repositories(service.root, transport=service.transport)
        self.store = repositories.video_jobs
        self.assets = repositories.assets
        self.previs = repositories.uploaded_previs
        self.adapters = video_adapters(service.config, service.transport)
        self.default_provider = service.config.get("VIDEO_DEFAULT_PROVIDER", "wan")
        if self.default_provider not in self.adapters:
            raise ValueError("VIDEO_DEFAULT_PROVIDER must name a registered video adapter")
        self.active = set()

    @property
    def client(self):
        return self.adapters[self.default_provider]

    def adapter_for(self, options):
        provider = options.get("provider", "wan")
        if provider not in self.adapters:
            raise VideoError("선택한 영상모델 어댑터가 등록되지 않았습니다.")
        return self.adapters[provider]

    async def native_scene_manifest(self, descriptor):
        """Prefer exported Blender data; uploaded MP4 explicitly falls back to 2D."""
        data = source_scene_reference(self.service, descriptor)
        reference = data.get("spatialManifestRef")
        if not reference:
            return None
        try:
            await self.service.client.start()
            if self.service.client.status["state"] != "connected":
                return None
            response = await self.service.client.call(reference["tool"], reference["arguments"])
            content = result_data(response) or {}
            download = content.get("download") or {}
            url = download.get("url")
            await public_https(url)
            chunks = []
            size = 0
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=False, trust_env=False, transport=PublicHTTPTransport()
            ) as http:
                async with http.stream("GET", url, headers=download.get("headers")) as result:
                    result.raise_for_status()
                    async for chunk in result.aiter_bytes():
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise GuideError("Blender 장면 정보가 너무 큽니다.")
                        chunks.append(chunk)
            manifest = json.loads(b"".join(chunks))
            if (
                manifest.get("version") != 1
                or not isinstance(manifest.get("objects"), list)
                or not isinstance(manifest.get("frames"), list)
            ):
                return None
            return manifest
        except (ValueError, KeyError, TypeError, RuntimeError, httpx.HTTPError, TimeoutError):
            return None

    def scene_prompt(self, source):
        if source.get("uploadedId"):
            return self.previs.get(source["uploadedId"])[0].get("scenePrompt", "")
        if source.get("jobId"):
            current = self.service.store.scene_prompt(source["jobId"])
            data = source_scene_reference(self.service, source)
            return current or data.get("scenePrompt") or data.get("request", "")
        path = self.root / "runtime/recovered" / (source["recoveredId"] + ".json")
        try:
            if path.is_symlink():
                return ""
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return (
                data.get("scenePrompt", "") if isinstance(data.get("scenePrompt", ""), str) else ""
            )
        except (ValueError, OSError):
            return ""

    def source(self, source):
        if not isinstance(source, dict):
            raise HTTPException(400, detail="프리비즈 결과를 선택하세요.")
        if set(source) == {"uploadedId"}:
            try:
                return {"path": self.previs.get(source["uploadedId"])[1]}
            except MediaExpired as error:
                raise HTTPException(410, detail=str(error)) from None
            except FileNotFoundError:
                raise HTTPException(404, detail="등록된 프리비즈 파일이 없습니다.") from None
            except VideoError as error:
                raise HTTPException(400, detail=str(error)) from None
        if (
            set(source) == {"recoveredId"}
            and isinstance(source["recoveredId"], str)
            and re.fullmatch(UUID, source["recoveredId"])
        ):
            directory = self.root / "runtime/recovered"
            path = directory / (source["recoveredId"] + ".mp4")
            record = self.service.archive.record("recovered-" + source["recoveredId"] + "-mp4")
            if record and is_expired(record["expiresAt"]):
                raise HTTPException(410, detail=str(MediaExpired()))
            if (
                path.is_file()
                and not path.is_symlink()
                and path.resolve().parent == directory.resolve()
            ):
                asset = self.service.archive.track(
                    "recovered-" + source["recoveredId"] + "-mp4",
                    path,
                    created=path.stat().st_mtime,
                )
                try:
                    return {"path": self.service.archive.path(asset["key"])}
                except MediaExpired as error:
                    raise HTTPException(410, detail=str(error)) from None
            raise HTTPException(404, detail="선택한 프리비즈 MP4가 없습니다.")
        if (
            set(source) == {"jobId", "mediaIndex"}
            and isinstance(source["jobId"], str)
            and re.fullmatch(UUID, source["jobId"])
            and type(source["mediaIndex"]) is int
            and source["mediaIndex"] >= 0
        ):
            job = self.service.store.get(source["jobId"])
            if (
                job
                and job["state"] == "completed"
                and source["mediaIndex"] < len(job["data"]["media"])
            ):
                item = job["data"]["media"][source["mediaIndex"]]
                if item.get("mediaType") == "video/mp4" and item.get("archive"):
                    try:
                        return {"path": self.service.archive.path(item["archive"]["key"])}
                    except MediaExpired as error:
                        raise HTTPException(410, detail=str(error)) from None
                    except FileNotFoundError:
                        raise HTTPException(
                            404, detail="저장된 프리비즈 파일이 없습니다."
                        ) from None
                if (
                    item.get("mediaType") == "video/mp4"
                    and item.get("download", {}).get("method") == "GET"
                ):
                    reference = item.get("artifactRef")
                    # Older saved jobs predate artifactRef: recover the read-only
                    # artifact request from their already-redacted tool events.
                    if not reference:
                        pending = None
                        candidates = []
                        for event in job["events"]:
                            value = event.get("value")
                            if event["type"] == "payload":
                                pending = (
                                    value
                                    if isinstance(value, dict)
                                    and tool_kind(value.get("tool", "")) == "get_artifact"
                                    else None
                                )
                            elif (
                                event["type"] == "response" and pending and isinstance(value, dict)
                            ):
                                data = result_data(value)
                                if (
                                    isinstance(data, dict)
                                    and data.get("ok")
                                    and data.get("artifact", {}).get("mediaType") == "video/mp4"
                                    and data.get("download", {}).get("method") == "GET"
                                ):
                                    candidates.append(
                                        (
                                            data["artifact"].get("name") or "previs.blend",
                                            {
                                                "tool": pending["tool"],
                                                "arguments": pending["arguments"],
                                            },
                                        )
                                    )
                                pending = None
                        videos = [
                            media
                            for media in job["data"]["media"]
                            if media.get("mediaType") == "video/mp4"
                        ]
                        ordinal = sum(
                            media.get("mediaType") == "video/mp4"
                            for media in job["data"]["media"][: source["mediaIndex"]]
                        )
                        if len(candidates) == len(videos) and all(
                            candidate[0] == media["name"]
                            for candidate, media in zip(candidates, videos)
                        ):
                            reference = candidates[ordinal][1]
                    return {"download": item["download"], "artifactRef": reference}
            if job is None:
                try:
                    return {
                        "path": self.service.archive.previs(source["jobId"], source["mediaIndex"])[
                            "path"
                        ]
                    }
                except MediaExpired as error:
                    raise HTTPException(410, detail=str(error)) from None
                except FileNotFoundError:
                    pass
                except ArchiveError as error:
                    raise HTTPException(502, detail=str(error)) from None
            raise HTTPException(404, detail="완료된 프리비즈 MP4 결과를 찾을 수 없습니다.")
        raise HTTPException(
            400,
            detail="프리비즈 결과 식별자가 올바르지 않습니다. 외부 URL 직접 입력은 지원하지 않습니다.",
        )

    async def download_source(self, source, descriptor, target):
        try:
            await download_video(
                source["download"]["url"],
                target,
                headers=source["download"].get("headers"),
                transport=self.service.transport,
            )
            return
        except VideoError as error:
            if error.status_code not in (401, 403, 404, 410):
                raise
            reference = source.get("artifactRef")
            if not reference or tool_kind(reference.get("tool", "")) != "get_artifact":
                raise VideoError(
                    "프리비즈 파일 링크가 만료되었습니다. 원본 파일을 복구하거나 프리비즈 결과를 다시 확인하세요."
                ) from None
        await self.service.client.start()
        if self.service.client.status.get("state") != "connected":
            raise VideoError(
                "원본 영상 링크 갱신에 Higgsfield 연결이 필요합니다. 연결 완료 후 다시 요청하세요."
            )
        response = await self.service.client.call(reference["tool"], reference["arguments"])
        data = result_data(response)
        download = data.get("download") if isinstance(data, dict) else None
        if (
            response.get("isError")
            or not isinstance(download, dict)
            or download.get("method") != "GET"
        ):
            raise VideoError(
                "원본 MP4 파일 링크를 갱신하지 못했습니다. Higgsfield 원본 결과를 확인하세요."
            )
        await download_video(
            download["url"],
            target,
            headers=download.get("headers"),
            transport=self.service.transport,
        )
        self.service.store.refresh_media(descriptor["jobId"], descriptor["mediaIndex"],
                                         download=download, artifactRef=reference)

    def result_asset(self, job):
        key = "wan-" + job["id"]
        record = self.service.archive.record(key)
        path = self.root / "runtime/wan" / job["id"] / "wan.mp4"
        if not record and path.is_file():
            record = self.service.archive.track(
                key, path, created=job["metadata"].get("completedAt", path.stat().st_mtime)
            )
        return record

    def public(self, job):
        asset = self.result_asset(job) if job["state"] == "completed" else None
        expired = bool(asset and is_expired(asset["expiresAt"]))
        message = (
            f"대기열에서 기다리는 중입니다. 최대 {self.service.max_concurrent_jobs}개를 동시에 실행합니다."
            if job["state"] == "queued"
            else job["message"]
        )
        if job["state"] == "completed" and job["options"].get("identityComparison"):
            message = "캐릭터 비교 영상이 생성되었습니다. 영구 보관됩니다."
        return {
            "id": job["id"],
            "state": job["state"],
            "message": message,
            "source": job["source"],
            "options": job["options"],
            "metadata": {**job["metadata"], **({"expiresAt":asset['expiresAt']} if asset else {})},
            "ai_usage": job["metadata"].get("ai_usage", []),
            "expired": expired,
            "expiresAt": asset["expiresAt"] if asset else None,
            "videoUrl": (
                "/api/video/jobs/" + job["id"] + "/video"
                if job["state"] == "completed" and asset and not expired
                else None
            ),
        }

    def ensure(self, job):
        history = getattr(self.service, "usage_history", None)
        if history is not None:
            try:
                history.record_video_source(job)
            except Exception as error:
                logging.getLogger("uvicorn.error").warning(
                    "job=%s 사용기록 저장 재시도 예정 (%s)", job["id"], type(error).__name__)
        owner = str(uuid4())
        if (
            job["state"] in TERMINAL
            or job["id"] in self.active
            or not self.store.claim(job["id"], owner=owner)
        ):
            return
        self.active.add(job["id"])
        task = asyncio.create_task(self.run(job["id"], owner))
        self.service.tasks.add(task)

        def done(finished):
            self.active.discard(job["id"])
            self.service.tasks.discard(finished)

        task.add_done_callback(done)

    async def run(self, job_id, owner):
        def update(**values):
            if not self.store.update(job_id, owner=owner, **values):
                raise LostLease()
            state = values.get("state")
            if values.get("message") and state in ("preparing", "running", "downloading"):
                logger.info("[작업 %s] 영상 진행 · %s", job_id, log_message(values["message"]))
            elif state == "failed":
                logger.error("[작업 %s] 영상 생성 실패 · %s", job_id,
                             log_message(values.get("message") or "상세 오류 없음"))

        async def heartbeat():
            while True:
                if not self.store.heartbeat(job_id, owner=owner):
                    return
                await asyncio.sleep(15)

        pulse = asyncio.create_task(heartbeat())
        job = self.store.get(job_id)
        options = job["options"]
        folder = self.root / "runtime/wan" / job_id
        reference = folder / (owner + "-previs.mp4")
        candidate = folder / (owner + "-wan.mp4")

        try:
            client = self.adapter_for(options)
            folder.mkdir(parents=True, exist_ok=True)
            logger.info("[작업 %s] 영상 실행 슬롯 대기", job_id)
            async with self.service.slots:
                if not job["provider_id"]:
                    job = await self._prepare_submission(job, owner, reference, folder, update)
                else:
                    logger.info("[작업 %s] 기존 영상 작업의 상태 조회 재개", job_id)
                await self._wait_for_result(job, owner, reference, candidate, folder, update)
        except asyncio.CancelledError:
            logger.warning("[작업 %s] 영상 처리 중단 · 서버 종료", job_id)
            self.store.update(job_id, owner=owner, lease=0)
            raise
        except LostLease:
            logger.warning("[작업 %s] 영상 실행 권한 만료 · 처리 중단", job_id)
            return
        except Exception as error:
            current = self.store.get(job_id)
            message = (
                str(error)
                if isinstance(error, (VideoError, PromptLengthError))
                else "영상 처리 중 오류가 발생했습니다. 저장된 작업 상태를 확인하세요."
            )
            if current["provider_id"]:
                logger.warning("[작업 %s] 영상 처리 오류 · 상태 조회 재개 예정 · %s · %s",
                               job_id, type(error).__name__, log_message(message))
                self.store.update(
                    job_id, owner=owner, state="running", message=message, lease=time.time() + 30
                )
            else:
                logger.error("[작업 %s] 영상 생성 실패 · %s · %s",
                             job_id, type(error).__name__, log_message(message))
                self.store.update(job_id, owner=owner, state="failed", message=message, lease=0)
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse
            reference.unlink(missing_ok=True)
            candidate.unlink(missing_ok=True)

    async def _analysis_request(self, path, body):
        api_key = self.service.config.get("OPENAI_API_KEY")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90, connect=15),
            transport=self.service.transport,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            return await request_openai(
                http, api_key, path, body, lambda *args: None, stage_label="영상 프롬프트·품질 검증"
            )

    async def _prepare_submission(self, job, owner, reference, folder, update):
        return await video_preparation._prepare_submission(self, job, owner, reference, folder, update)

    async def _review_completed_video(self, job, owner, reference, candidate, folder, update):
        return await video_results._review_completed_video(self, job, owner, reference, candidate, folder, update)

    async def _wait_for_result(self, job, owner, reference, candidate, folder, update):
        return await video_results._wait_for_result(self, job, owner, reference, candidate, folder, update)

    async def _prepare_spatial_guides(
        self,
        *,
        job,
        owner,
        reference,
        asset_files,
        guide_directory,
        scene_prompt,
        generation_prompt,
        metadata,
        update,
    ):
        return await video_preparation._prepare_spatial_guides(self, job=job, owner=owner, reference=reference, asset_files=asset_files, guide_directory=guide_directory, scene_prompt=scene_prompt, generation_prompt=generation_prompt, metadata=metadata, update=update)


