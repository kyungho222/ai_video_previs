"""Provider-neutral video HTTP endpoints."""

import asyncio
import json
from pathlib import Path
import re
import time

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    File,
    Form,
    UploadFile,
)
from fastapi.responses import FileResponse
from ai_video.model_gateway import canonical_model
from .media_archive import MediaExpired, ArchiveError
from .pipeline_log import log_request, logger
from .scene_flow import FIRST_SCENE_CONTINUITY
from .spatial_guides import spatial_guide_budget
from .video_errors import UUID, MAX_AUDIO_BYTES, MAX_IMAGE_BYTES, AssetError, VideoError
from .video_media import LimitedUploadRoute, inspect_video, create_previs_upload_router
from .video_prompt import DEFAULT_VIDEO_PROMPT, PROMPT_POLICY_VERSION

from .camera_reference import scene_project_id, source_scene_reference
from .work_settings import request_work_metadata, effective_chat_model, DEFAULT_VIDEO_ANALYSIS_MODEL
from .usage_registration import prepare_video_usage
from .video_models import validate_video_input
from .previs_timeline import bind_source_identity
from .prompt_filter import check_local_terms, filter_user_prompts, PromptFilterError


from .video_results import TERMINAL

def create_video_router(service):
    from .video_generation import VideoManager
    manager = VideoManager(service)
    router = APIRouter(route_class=LimitedUploadRoute)
    router.manager = manager
    router.include_router(create_previs_upload_router(manager.previs))

    def default_region(capabilities):
        preferred = service.config.get("VIDEO_REGION") or service.config.get("WAN_REGION")
        return preferred if preferred in capabilities.regions else capabilities.regions[0]

    @router.post("/api/video/assets", status_code=201)
    async def upload_asset(file: UploadFile = File(...), role: str = Form("object"),
                           sourceObjectId: str | None = Form(None), label: str | None = Form(None),
                           appearance: str | None = Form(None)):
        try:
            limit = MAX_AUDIO_BYTES if role == "audio" else MAX_IMAGE_BYTES
            content = await file.read(limit + 1)
            options = {}
            if sourceObjectId is not None: options['source_object_id'] = sourceObjectId
            if label is not None: options['label'] = label
            if appearance is not None: options['appearance'] = appearance
            asset = await asyncio.to_thread(manager.assets.save, content, role, file.filename, **options)
            return {**asset, "url": "/api/video/assets/" + asset["id"]}
        except AssetError as error:
            raise HTTPException(400, detail=str(error)) from None
        finally:
            await file.close()

    @router.get("/api/video/assets/{asset_id}")
    async def asset_file(asset_id: str):
        try:
            asset_metadata, path = manager.assets.get(asset_id)
        except AssetError as error:
            raise HTTPException(400, detail=str(error)) from None
        except FileNotFoundError:
            raise HTTPException(404, detail="선택한 에셋이 없습니다.") from None
        return FileResponse(
            path,
            media_type=asset_metadata.get("mediaType", "image/jpeg"),
            headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
        )

    @router.get("/api/video/config")
    async def config(provider: str | None = None):
        identifier = provider or manager.default_provider
        if identifier not in manager.adapters:
            raise HTTPException(400, detail="등록된 영상모델을 선택하세요.")
        adapter = manager.adapters[identifier]
        capabilities = adapter.capabilities
        return {
            "configured": adapter.configured,
            "provider": identifier,
            "model": capabilities.model,
            "models": [
                {
                    "provider": name,
                    "model": item.capabilities.model,
                    "label": item.capabilities.label,
                    "configured": item.configured,
                }
                for name, item in manager.adapters.items()
            ],
            "maxConcurrentJobs": service.max_concurrent_jobs,
            "defaultPrompt": DEFAULT_VIDEO_PROMPT,
            "promptPolicyVersion": PROMPT_POLICY_VERSION,
            "region": default_region(capabilities),
            "regions": list(capabilities.regions),
            "resolutions": list(capabilities.resolutions),
            "referenceModes": list(capabilities.reference_modes),
            "spatialGuidesConfigured": bool(service.config.get("OPENAI_API_KEY")),
            "identityComparisonModes": (
                (["compare_references"] if "video" in capabilities.reference_modes else [])
                + (
                    ["compare_first_frame"]
                    if "spatial_motion" in capabilities.reference_modes
                    and "references" in capabilities.spatial_inputs
                    else []
                )
            ),
            "defaultReferenceMode": "video" if "video" in capabilities.reference_modes else "images",
            "defaultCharacterMotion": "independent",
            "cameraReferenceRequiresScene": True,
            "defaultGuideSelection": "key_scenes_v1",
            "referenceImageLimit": capabilities.reference_image_limit,
            "referenceAudioLimit": capabilities.reference_audio_limit,
            "spatialInputFormats": list(capabilities.spatial_inputs),
            "message": (
                "영상 참조 입력 설정을 확인하세요."
                if adapter.configured
                else "선택한 영상모델의 API 인증을 설정하세요."
            ),
        }

    @router.get("/api/video/sources")
    async def sources():
        results = []
        for job_id, data in service.store.completed_sources():
            media = []
            for index, item in enumerate(data.get("media", [])):
                if item.get("mediaType") == "video/mp4":
                    archived = item.get("archive")
                    if archived:
                        try:
                            service.archive.path(archived["key"])
                        except (MediaExpired, FileNotFoundError):
                            continue
                        archived = service.archive.record(archived['key']) or archived
                    media.append(
                        {
                            "name": item["name"],
                            "mediaType": "video/mp4",
                            "url": f"/api/jobs/{job_id}/media/{index}",
                            **({"expiresAt": archived["expiresAt"]} if archived else {}),
                            "videoSource": {"jobId": job_id, "mediaIndex": index},
                            "wanSource": {"jobId": job_id, "mediaIndex": index},
                        }
                    )
                    media[-1]["scenePrompt"] = service.store.scene_prompt(job_id)
                    media[-1]["sourceDuration"] = data.get("duration", 5)
                    if data.get("workSettings"):
                        media[-1]["workSettings"] = data["workSettings"]
            if media:
                results.append({"message": "저장된 프리비즈 · " + job_id[:8], "media": media, "intentQuality": data.get("intentQuality")})
        known = {
            (item["videoSource"]["jobId"], item["videoSource"]["mediaIndex"])
            for result in results
            for item in result["media"]
        }
        archived = await asyncio.to_thread(service.archive.previs_results)
        for item in archived:
            job_id, index = item["jobId"], item["mediaIndex"]
            if (job_id, index) in known:
                continue
            try:
                info = await asyncio.to_thread(inspect_video, item["path"])
            except (VideoError, OSError):
                continue
            source = {"jobId": job_id, "mediaIndex": index}
            reference = source_scene_reference(service, source)
            work_settings = reference.get("workSettings")
            results.append(
                {
                    "message": "보관된 프리비즈 · " + job_id[:8],
                    "intentQuality": reference.get("intentQuality"),
                    "media": [
                        {
                            "name": f"previs-{job_id}-{index}.mp4",
                            "mediaType": "video/mp4",
                            "url": f"/api/jobs/{job_id}/media/{index}",
                            "expiresAt": item["expiresAt"],
                            "videoSource": source,
                            "wanSource": source,
                            "scenePrompt": service.store.scene_prompt(job_id),
                            "sourceDuration": info["duration"],
                            **({"workSettings": work_settings} if work_settings else {}),
                        }
                    ],
                }
            )
        return {"results": results}

    @router.post("/api/video/generate", status_code=202)
    @log_request("영상 생성")
    async def generate(request: Request):
        if len(await request.body()) > 65536:
            raise HTTPException(413, detail="입력이 너무 깁니다.")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail="JSON 요청 형식을 확인하세요.") from None
        if not isinstance(body, dict):
            raise HTTPException(400, detail="영상 생성 설정을 입력하세요.")
        request_id = body.get("requestId")
        if type(body.get('allowUnverifiedGuides', False)) is not bool:
            raise HTTPException(400, detail='가이드 강제 진행 옵션은 boolean이어야 합니다.')
        prompt, duration, resolution = (
            body.get(k) for k in ("prompt", "duration", "resolution")
        )
        if not isinstance(request_id, str) or not re.fullmatch(UUID, request_id):
            raise HTTPException(400, detail="요청 ID가 올바르지 않습니다.")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000:
            raise HTTPException(400, detail="영상 지시를 1~8000자로 입력하세요.")
        try:
            check_local_terms(prompt)
        except PromptFilterError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        if (
            type(duration) is not int
            or duration <= 0
            or not isinstance(resolution, str)
            or ("region" in body and not isinstance(body["region"], str))
        ):
            raise HTTPException(
                400, detail="영상 길이(양의 정수), 해상도, API 키 리전을 확인하세요."
            )
        source = body.get("source")
        resolved_source = manager.source(source)
        previous_job = manager.store.get(request_id)
        try:
            work_metadata = request_work_metadata(body)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        work_settings = (
            previous_job["options"].get("workSettings") if previous_job
            else source_scene_reference(service, source).get("workSettings")
        )
        default_chat_model = canonical_model(service.config.get("VIDEO_PROMPT_MODEL") or DEFAULT_VIDEO_ANALYSIS_MODEL)
        chat_model = body.get("chatModel", previous_job["options"].get("chatModel", default_chat_model)
                              if previous_job else default_chat_model)
        if work_settings:
            chat_model = work_settings["model"]
        elif work_metadata and previous_job is None:
            work_settings = {"model": chat_model, **work_metadata}
        chat_model = effective_chat_model(service, chat_model)
        if work_settings:
            work_settings = {**work_settings, "model": chat_model}
        if not isinstance(chat_model, str) or chat_model not in getattr(service, "chat_models", [default_chat_model]):
            raise HTTPException(400, detail="허용된 채팅 모델을 선택하세요.")
        provider = body.get(
            "provider",
            (
                previous_job["options"].get("provider", "wan")
                if previous_job
                else manager.default_provider
            ),
        )
        if not isinstance(provider, str) or provider not in manager.adapters:
            raise HTTPException(400, detail="등록된 영상모델을 선택하세요.")
        if previous_job and provider != previous_job["options"].get("provider", "wan"):
            raise HTTPException(409, detail="기존 요청 ID의 영상모델은 변경할 수 없습니다.")
        adapter = manager.adapters[provider]
        capabilities = adapter.capabilities
        saved_region = previous_job["options"].get("region") if previous_job else None
        region = body.get("region", saved_region or default_region(capabilities))
        if body.get("model", capabilities.model) != capabilities.model:
            raise HTTPException(400, detail="선택한 공급사에 등록된 모델을 확인하세요.")
        if not adapter.configured:
            raise HTTPException(503, detail="선택한 영상모델의 API 인증이 필요합니다.")
        try:
            resolved_assets = manager.assets.resolve(body.get("assets", []))
        except AssetError as error:
            raise HTTPException(400, detail=str(error)) from None
        except FileNotFoundError:
            raise HTTPException(
                404, detail="선택한 에셋이 없습니다. 파일을 다시 선택하세요."
            ) from None
        from .asset_roles import partition_assets
        asset_groups=partition_assets(resolved_assets)
        # Preserve visual image numbering; audio belongs only to the audio path.
        assets = [asset for asset in resolved_assets if asset['role'] != 'audio']
        from .asset_diagnostics import asset_rows, log_asset_event
        log_asset_event('request_assets',requestId=request_id,originalAssetCount=len(assets),
                        registeredAssetCount=len(resolved_assets),assets=asset_rows(assets))
        audio_assets = asset_groups['audio']
        audio_mode = "bgm" if audio_assets else "generated"
        if previous_job and "audioMode" not in previous_job["options"]:
            audio_mode = None
        if len(audio_assets) > capabilities.reference_audio_limit:
            raise HTTPException(400, detail="선택한 영상모델의 참고 음원 지원 범위를 확인하세요.")
        options = {
            "prompt": prompt.strip(),
            "duration": duration,
            "resolution": resolution,
            "region": region,
            "allowUnverifiedGuides": body.get('allowUnverifiedGuides', False),
        }
        if previous_job is None or "provider" in previous_job["options"]:
            options.update(provider=provider, model=capabilities.model)
        if previous_job is None or "chatModel" in previous_job["options"]:
            options["chatModel"] = chat_model
        if work_settings:
            options["workSettings"] = work_settings
        usage_history = getattr(service, 'usage_history', None)
        usage_source = ((usage_history.get(source.get('jobId')) if usage_history else None)
                        or source_scene_reference(service, source) or {})
        await prepare_video_usage(service, options, body, source=usage_source, previous_job=previous_job,
                                  settings=work_settings, metadata=work_metadata, request_id=request.state.pipeline_id)
        options["scenePrompt"] = manager.scene_prompt(source)
        requested_mode = body.get("referenceMode", "auto")
        if requested_mode == "auto":
            if previous_job:
                previous_options = previous_job["options"]
                previous_comparison = previous_options.get("identityComparison")
                requested_mode = (
                    "compare_first_frame"
                    if previous_comparison == "B"
                    else (
                        "compare_references"
                        if previous_comparison == "A"
                        else "text_continuity" if previous_options.get("sceneFlowPolicy") == FIRST_SCENE_CONTINUITY
                        else "text_endpoints" if previous_options.get("previsTranscript")
                        else previous_options.get("referenceMode", "video")
                    )
                )
            else:
                requested_mode = "text_endpoints"
        include_video = body.get(
            "referenceVideo",
            previous_job["options"].get("referenceVideo", False) if previous_job else True,
        )
        if type(include_video) is not bool:
            raise HTTPException(400, detail="MP4 전달 설정을 확인하세요.")
        mode_aliases = {
            "text_continuity": "spatial",
            "text_endpoints": "spatial",
            "compare_references": "video" if include_video else "images",
            "compare_first_frame": "spatial_motion" if include_video else "spatial",
        }
        if not isinstance(requested_mode, str) or requested_mode not in (
            "auto",
            "images",
            "video",
            "spatial",
            "spatial_motion",
            "text_endpoints",
            "text_continuity",
            "compare_references",
            "compare_first_frame",
        ):
            raise HTTPException(400, detail="참고 입력 방식을 확인하세요.")
        mode = mode_aliases.get(requested_mode, requested_mode)
        text_endpoints = requested_mode == "text_endpoints"
        if text_endpoints or requested_mode == "text_continuity":
            options["previsTranscript"] = "camera_timeline_v1"
        if requested_mode == "text_continuity":
            options["sceneFlowPolicy"] = FIRST_SCENE_CONTINUITY
        if requested_mode in ("compare_references", "compare_first_frame"):
            if not service.config.get("OPENAI_API_KEY"):
                raise HTTPException(
                    503, detail="캐릭터 비교에는 서버의 OPENAI_API_KEY 설정이 필요합니다."
                )
            if not {"character", "background"}.issubset({asset["role"] for asset in assets}):
                raise HTTPException(
                    400, detail="캐릭터 비교에는 캐릭터와 배경 에셋이 각각 필요합니다."
                )
            options["identityComparison"] = "A" if requested_mode == "compare_references" else "B"
            if previous_job is None or "referenceVideo" in previous_job["options"]:
                options["referenceVideo"] = include_video
        if mode == "images" and not {"character", "background"}.issubset(
            {asset["role"] for asset in assets}
        ):
            raise HTTPException(
                400,
                detail="에셋 전용 생성에는 캐릭터와 배경 이미지를 각각 추가하세요. 프리비즈 MP4는 전달하지 않습니다.",
            )
        character_motion = body.get(
            "characterMotion",
            previous_job["options"].get("characterMotion", "reference") if previous_job else "independent",
        )
        if character_motion not in ("independent", "reference"):
            raise HTTPException(400, detail="캐릭터 동작 참조 설정을 확인하세요.")
        if previous_job is None or "characterMotion" in previous_job["options"]:
            options["characterMotion"] = character_motion
        explicit_project = body.get("sceneProjectId")
        if explicit_project:
            if not isinstance(explicit_project, str):
                raise HTTPException(400, detail="원본 3D 프로젝트 ID를 확인하세요.")
            explicit_project = explicit_project.strip()
            link = re.fullmatch(r"https://higgsfield\.ai/3d-jutsu/(" + UUID + r")/?", explicit_project)
            if link:
                explicit_project = link[1]
            if not re.fullmatch(UUID, explicit_project):
                raise HTTPException(400, detail="원본 3D 프로젝트 주소 또는 ID를 확인하세요.")
            options["sceneProjectId"] = explicit_project
        elif previous_job and previous_job["options"].get("sceneProjectId"):
            options["sceneProjectId"] = previous_job["options"]["sceneProjectId"]
        if mode in ("video", "spatial_motion") and character_motion == "independent":
            try:
                project_id = scene_project_id(service, source, options.get("sceneProjectId"))
            except VideoError as error:
                raise HTTPException(422, detail=str(error)) from None
            if previous_job is None:
                options["sceneProjectId"] = project_id
        options["referenceMode"] = mode
        if mode in ("spatial", "spatial_motion"):
            if not service.config.get("OPENAI_API_KEY"):
                raise HTTPException(
                    503,
                    detail="공간 가이드 생성용 OPENAI_API_KEY가 없습니다. 기존 MP4 방식을 선택하세요.",
                )
            spatial_input = (
                "first_frame" if text_endpoints else
                "references" if requested_mode == "text_continuity" else
                ("references" if include_video else "first_frame")
                if options.get("identityComparison") == "B"
                else body.get("spatialInput", "references")
            )
            if audio_assets and audio_mode != "bgm" and spatial_input != "references":
                raise HTTPException(400, detail="참고 음원을 쓰려면 시작·종료 이미지 대신 참조 방식으로 바꾸세요.")
            count = body.get("spatialGuideCount", "auto")
            if spatial_input not in capabilities.spatial_inputs or not (
                count == "auto" or type(count) is int and 1 <= count <= 64
            ):
                raise HTTPException(400, detail="공간 가이드 형식과 최대 수를 확인하세요.")
            if not assets:
                raise HTTPException(400, detail="공간 가이드에 적용할 에셋을 등록하세요.")
            try:
                count = (
                    1
                    if options.get("identityComparison") == "B" or spatial_input == "first_frame"
                    else (
                        2
                        if spatial_input == "first_last"
                        else spatial_guide_budget(
                            len(assets), min(3, capabilities.reference_image_limit - len(assets))
                            if requested_mode == "text_continuity" and count == "auto" else count,
                            image_limit=capabilities.reference_image_limit
                        )
                    )
                )
            except VideoError as error:
                raise HTTPException(400, detail=str(error)) from None
            try:
                adapter.build_media(
                    mode,
                    spatial_input,
                    [{"url": "asset"} for _ in assets],
                    [{"url": "guide"} for _ in range(count)],
                    "motion" if mode == "spatial_motion" else None,
                )
            except VideoError as error:
                raise HTTPException(400, detail=str(error)) from None
            options.update(spatialInput=spatial_input, spatialGuideCount=count)
            previous = manager.store.get(request_id)
            if previous is None or "spatialGuideSelection" in previous["options"]:
                options["spatialGuideSelection"] = (
                    previous["options"]["spatialGuideSelection"] if previous else
                    "uniform" if requested_mode == "text_continuity" else "key_scenes_v1"
                )
        try:
            validate_video_input(
                adapter,
                mode,
                options.get("spatialInput", "references"),
                duration,
                resolution,
                region,
                (
                    len(assets)
                    if mode in ("images", "video")
                    or options.get("spatialInput", "references") == "references"
                    else 0
                ),
            )
        except VideoError as error:
            raise HTTPException(400, detail=str(error)) from None
        if assets:
            options["assets"] = assets
        options['assetGroups']={role:[asset['id'] for asset in items] for role,items in asset_groups.items()}
        if audio_assets:
            options["audioAssets"] = audio_assets
        if audio_mode:
            options["audioMode"] = audio_mode
        try:
            prompt_filter = await filter_user_prompts(
                prompt, options["scenePrompt"], model=chat_model, api_key=service.config.get("OPENAI_API_KEY"),
                transport=getattr(service, "transport", None),
                cached=(previous_job or {}).get("metadata",{}).get("promptFilter"),
            )
        except PromptFilterError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        try:
            initial_metadata = {"ai_usage":prompt_filter.get("ai_usage",[])}
            if previous_job is None:
                initial_metadata["promptFilter"] = prompt_filter
                initial_metadata["sourceIdentity"] = await asyncio.to_thread(
                    bind_source_identity, source, resolved_source.get("path"),
                )
            job, _ = manager.store.create(request_id, source, options, initial_metadata=initial_metadata)
        except (ValueError, VideoError) as error:
            raise HTTPException(409, detail=str(error)) from None
        manager.ensure(job)
        logger.info("[요청 %s] [작업 %s] 영상 생성 요청 처리 완료 · 상태 %s",
                    request.state.pipeline_id, job["id"], job["state"])
        return manager.public(job)

    @router.get("/api/video/jobs")
    async def jobs():
        active = manager.store.unfinished()
        rows = active + [row for row in manager.store.recent() if row["state"] in TERMINAL]
        for row in rows:
            manager.ensure(row)
        return {"jobs": [manager.public(row) for row in rows]}

    @router.get("/api/video/jobs/{job_id}/scenario-snapshots/{index}")
    async def scenario_snapshot_image(job_id: str, index: int):
        row = manager.store.get(job_id) if re.fullmatch(UUID, job_id) else None
        snapshot = next((item for item in (row["metadata"].get("scenarioSnapshots") or [])
                         if item["index"] == index), None) if row else None
        if snapshot is None:
            raise HTTPException(404, detail="시나리오 스냅샷을 찾을 수 없습니다.")
        try:
            path = service.archive.path(snapshot["archiveKey"])
        except MediaExpired as error:
            raise HTTPException(410, detail=str(error)) from None
        except (FileNotFoundError, ArchiveError):
            raise HTTPException(404, detail="시나리오 스냅샷을 찾을 수 없습니다.") from None
        return FileResponse(path, media_type="image/png", headers={"cache-control": "no-store"})

    @router.get("/api/video/jobs/{job_id}/guides/{index}")
    async def spatial_guide_image(job_id: str, index: int):
        if not re.fullmatch(UUID, job_id):
            raise HTTPException(404, detail="공간 가이드를 찾을 수 없습니다.")
        row = manager.store.get(job_id)
        guides = (row["metadata"].get("spatialGuides") or {}).get("guides", []) if row else []
        guide = next((item for item in guides if item.get("index") == index), None)
        if guide is None or not re.fullmatch(r"spatial-[0-9]{1,2}\.png", guide.get("name", "")):
            raise HTTPException(404, detail="공간 가이드를 찾을 수 없습니다.")
        try:
            path = service.archive.safe_path(
                Path("runtime/wan") / job_id / "guides" / guide["name"]
            )
        except ArchiveError:
            raise HTTPException(404, detail="공간 가이드 경로가 올바르지 않습니다.") from None
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, detail="공간 가이드 이미지가 아직 준비되지 않았습니다.")
        key = f"guide-{job_id}-{path.parent.name}-{path.name}"
        try:
            record = service.archive.track(key, path, created=row["created"], reuse_path=True)
            path = service.archive.path(record["key"])
        except MediaExpired as error:
            raise HTTPException(410, detail=str(error)) from None
        except FileNotFoundError:
            raise HTTPException(
                404, detail="공간 가이드 이미지가 아직 준비되지 않았습니다."
            ) from None
        return FileResponse(path, media_type="image/png", headers={"cache-control": "no-store"})

    @router.get("/api/video/jobs/{job_id}")
    async def job(job_id: str):
        row = manager.store.get(job_id)
        if row is None:
            raise HTTPException(404, detail="영상 작업이 없습니다.")
        manager.ensure(row)
        return manager.public(row)

    @router.get("/api/video/jobs/{job_id}/video")
    async def video(job_id: str):
        row = manager.store.get(job_id)
        if not row or row["state"] != "completed" or not re.fullmatch(UUID, job_id):
            raise HTTPException(404, detail="완료된 영상이 없습니다.")
        asset = manager.result_asset(row)
        if not asset:
            raise HTTPException(404, detail="저장된 영상이 없습니다.")
        try:
            path = service.archive.path(asset["key"])
        except MediaExpired as error:
            raise HTTPException(410, detail=str(error)) from None
        except FileNotFoundError:
            raise HTTPException(404, detail="저장된 영상이 없습니다.") from None
        return FileResponse(path, media_type="video/mp4", headers={"cache-control": "no-store"})

    from fastapi.routing import APIRoute

    for route in list(router.routes):
        if isinstance(route, APIRoute) and route.path.startswith("/api/video/"):
            router.add_api_route(
                route.path.replace("/api/video/", "/api/wan/", 1),
                route.endpoint,
                methods=sorted(route.methods),
                status_code=route.status_code,
                route_class_override=type(route),
            )
    return router
