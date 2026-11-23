"""Prepare reference media, asset guides and the final model submission."""

import asyncio
import json
import logging
import re
import shutil

from .spatial_guides import (
    GuideError,
    SpatialGuideBuilder,
    OpenAIGuideImageEditor,
    spatial_prompt_context,
    scene_text_without_asset_indices,
    bind_spatial_prompt,
    prepare_asset_parts,
)
from .video_errors import MAX_REFERENCE_BYTES, VideoError, PromptLengthError
from .video_media import inspect_video
from .reference_audio import upload_reference_audio
from .bgm import VOICE_ONLY
from .scene_flow import FIRST_SCENE_CONTINUITY, SCENE_FLOW_RULES, continuous_guides
from .creative_policy import KOREAN_ATMOSPHERE_RULE
from .video_prompt import (
    PROMPT_POLICY_VERSION,
    PREVIS_RULES,
    CAMERA_GUIDE_RULES,
    optimize_video_prompt,
    reference_prompt_overhead,
    fit_video_prompt,
)

from .camera_reference import scene_project_id, prepare_camera_reference, source_scene_reference
from .media_archive import ArchiveError
from .scenario_snapshots import scenario_frames, capture_scenario_snapshots, limit_scenario_frames
from .work_settings import video_analysis_model
from .video_models import validate_video_input
from .video_validation import validate_video_request
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
from .prompt_filter import apply_presentation_rules, attach_presentation_rules


from .video_results import LostLease

def verified_guide_inputs(mode, spatial_input, guides, asset_count, image_limit):
    accepted = [guide for guide in guides if guide["status"] == "verified"]
    if len(accepted) == len(guides) and accepted:
        return mode, spatial_input, accepted
    if spatial_input == "first_last" and accepted and accepted[0]["index"] == 0:
        return mode, "first_frame", accepted
    if spatial_input != "references":
        accepted = accepted[: max(0, image_limit - asset_count)]
    if not accepted:
        mode = "video" if mode == "spatial_motion" else "images"
    return mode, "references", accepted


async def _prepare_submission(self, job, owner, reference, folder, update):
    job_id = job["id"]
    options = job["options"]
    scene_flow = options.get("sceneFlowPolicy")
    api_key = self.service.config.get("OPENAI_API_KEY")
    review_model = video_analysis_model(self.service, options)
    presentation = job.get("metadata", {}).get("promptFilter", {}).get("generationRules", "")

    async def optimize_request(path, body):
        return await self._analysis_request(path, apply_presentation_rules(path, body, presentation))
    client = self.adapter_for(options)
    capabilities = client.capabilities
    if job["state"] not in ("queued", "preparing"):
        raise VideoError(
            "중단된 영상 생성 요청의 접수 여부를 확인할 수 없습니다. 자동 재요청하지 않았습니다. 공급사 작업 내역을 확인하세요."
        )
    mode = options.get("referenceMode", "video")
    comparison = options.get("identityComparison")
    spatial = mode in ("spatial", "spatial_motion")
    spatial_input = options.get("spatialInput", "references")
    sends_video = mode in ("video", "spatial_motion")
    update(
        state="preparing",
        message=(
            "공간 가이드와 선택적 동작 참조를 준비합니다."
            if spatial
            else "참고 입력을 준비합니다. "
            + (
                "프리비즈 MP4를 전달합니다."
                if sends_video
                else "프리비즈 MP4는 전달하지 않습니다."
            )
        ),
    )
    source = self.source(job["source"])
    if "path" in source:
        if source["path"].stat().st_size > MAX_REFERENCE_BYTES:
            raise VideoError("참고 MP4는 100MB 이하여야 합니다.")
        await asyncio.to_thread(shutil.copyfile, source["path"], reference)
    else:
        await self.download_source(source, job["source"], reference)
    try:
        metadata = await asyncio.to_thread(
            inspect_video, reference, max_duration=None, min_fps=0, check_dimensions=False
        )
    except VideoError as error:
        metadata = {"inspectionWarning": str(error)}
    metadata = {**job.get("metadata", {}), **metadata}
    metadata["sourceIdentity"] = await asyncio.to_thread(
        bind_source_identity, job["source"], reference, metadata.get("sourceIdentity"),
    )
    try:
        scenes = scenario_frames(source_scene_reference(self.service, job["source"]))
        if scenes and not metadata.get("scenarioSnapshots"):
            directory = self.service.archive.safe_path((folder / (owner + "-scenes")).relative_to(self.root))
            captures = await asyncio.to_thread(capture_scenario_snapshots, reference, directory, scenes)
            snapshots = []
            for scene, path in captures:
                key = f"scenario-snapshot-{job_id}-{owner}-{scene['index']}"
                self.service.archive.track(key, path, created=job["created"])
                snapshots.append({**scene, "url": f"/api/video/jobs/{job_id}/scenario-snapshots/{scene['index']}",
                                  "archiveKey": key})
            metadata["scenarioSnapshots"] = snapshots
            metadata.pop("scenarioSnapshotWarning", None)
    except (ArchiveError, GuideError, OSError, ValueError, KeyError, TypeError) as error:
        logging.getLogger(__name__).warning("Scenario snapshots unavailable for job %s (%s)", job_id, type(error).__name__)
        metadata["scenarioSnapshotWarning"] = "프리비즈에서 씬별 스냅샷을 준비하지 못했습니다."
    update(metadata=metadata)
    model_reference = reference
    separated = sends_video and options.get("characterMotion") == "independent"
    if separated:
        project_id = scene_project_id(self.service, job["source"], options.get("sceneProjectId"))
        def save_camera_reference(record):
            metadata["cameraReference"] = record
            update(metadata=metadata)
        model_reference = await prepare_camera_reference(
            self.service.client, project_id, folder / (owner + "-camera-staging.mp4"),
            metadata.get("cameraReference"), save_camera_reference,
            lambda message: update(message=message),
        )
        try:
            self.service.archive.track("camera-guide-" + job_id + "-" + owner,
                                       model_reference, created=job["created"])
        except BaseException:
            model_reference.unlink(missing_ok=True)
            raise
    metadata["inputPolicy"] = "camera-staging-separated-v1" if separated else "original-files-v1"

    update(metadata=metadata, message="영상 프롬프트를 통합·최적화합니다.")
    scene_prompt = options.get("scenePrompt", "")
    generation_prompt = options["prompt"]
    bundle = None
    guide_context = None
    guide_directory = folder / "guides"
    requested_spatial_input = spatial_input
    asset_files = []
    for asset in options.get("assets", []):
        current, path = self.assets.get(asset["id"])
        asset_files.append({**current, "path": path})
    camera_transcript = None
    if options.get("previsTranscript") == "camera_timeline_v1":
        timeline_directory = folder / "camera-timeline"
        try:
            transcript = await describe_previs(
                reference, timeline_directory, request=optimize_request, model=review_model,
                brief=scene_prompt + "\n" + generation_prompt, assets=asset_files,
                native=await self.native_scene_manifest(job["source"]),
                cached=metadata.get("previsTranscript"),
                progress=lambda message: update(message=message),
            )
        finally:
            for path in timeline_directory.glob("layout-*.png"):
                safe = self.service.archive.safe_path(path.relative_to(self.root))
                self.service.archive.track(f"timeline-{job_id}-{path.name}", safe,
                                           created=job["created"], reuse_path=True)
        if transcript.get("sourceSha256") and transcript["sourceSha256"] != metadata["sourceIdentity"]["sha256"]:
            raise VideoError("카메라 분석 원본과 선택한 프리비즈 MP4가 다릅니다.")
        metadata["previsTranscript"] = transcript
        metadata["inputPolicy"] = scene_flow or "camera-text-endpoints-v1"
        camera_transcript = timeline_prompt(transcript["plan"], transcript["sourceDuration"],
                                            options["duration"], scene_flow_policy=scene_flow)
        update(metadata=metadata, message=("촬영 타임라인 분석 완료 · 씬 참조 이미지를 구성합니다."
                                          if scene_flow else "촬영 타임라인 분석 완료 · 시작·종료 이미지를 구성합니다."))
    if comparison == "A":
        update(message="캐릭터 참조 이미지를 멤버별로 준비합니다.")
        guide_directory.mkdir(parents=True, exist_ok=True)
        parts = metadata.get("characterAssetParts")
        if parts is None:
            analyzer = SpatialGuideBuilder(
                optimize_request,
                None,
                model=review_model,
            )
            parts = await analyzer.detect_asset_parts(asset_files)
        if len(parts) > capabilities.reference_image_limit:
            raise GuideError(
                f"참조 이미지는 선택 모델 기준 최대 {capabilities.reference_image_limit}장입니다."
            )
        asset_files = await asyncio.to_thread(
            prepare_asset_parts,
            asset_files,
            parts,
            guide_directory,
            image_constraints=capabilities.image_constraints,
        )
        for asset in asset_files:
            if "bbox" in asset:
                path = self.service.archive.safe_path(asset["path"].relative_to(self.root))
                key = f"guide-{job_id}-{guide_directory.name}-{path.name}"
                self.service.archive.track(key, path, created=job["created"], reuse_path=True)
                asset["mediaType"] = "image/png"
        metadata["characterAssetParts"] = parts
        update(metadata=metadata)
    if spatial:
        bundle, mode, spatial_input = await self._prepare_spatial_guides(
            job=job,
            owner=owner,
            reference=model_reference,
            asset_files=asset_files,
            guide_directory=guide_directory,
            scene_prompt=scene_prompt,
            generation_prompt=generation_prompt,
            metadata=metadata,
            update=update,
        )
        guide_context = spatial_prompt_context(bundle, spatial_input)
        if scene_flow == FIRST_SCENE_CONTINUITY:
            guide_context.update(sceneFlowPolicy=scene_flow, sceneFlowRules=SCENE_FLOW_RULES)
        if spatial_input != "references":
            generation_prompt = scene_text_without_asset_indices(generation_prompt, bundle)
            scene_prompt = scene_text_without_asset_indices(scene_prompt, bundle)
    validate_video_input(
        client,
        mode,
        spatial_input,
        options["duration"],
        options["resolution"],
        options["region"],
        len(asset_files) if spatial_input == "references" or not spatial else 0,
    )
    parts = metadata.get("characterAssetParts")
    if bundle is None and parts is None and api_key and any(asset["role"] == "character" for asset in asset_files):
        parts = await SpatialGuideBuilder(optimize_request, None, model=review_model).detect_asset_parts(asset_files)
        metadata["characterAssetParts"] = parts
        update(metadata=metadata)
    records = character_records(options.get("assets", []), bundle, parts=parts)
    cast = self.store.register_characters(records) if records else []

    def direction_owner_check():
        if not self.store.heartbeat(job_id, owner=owner):
            raise LostLease()

    def save_direction(record):
        metadata["characterDirection"] = record
        update(metadata=metadata)

    prompt_assets = ([{"role": asset["role"]} for asset in options.get("assets", [])]
                     if not spatial or spatial_input == "references" else [])
    prompt_mode = "images" if spatial else mode
    spatial_suffix = ""
    if spatial and (sends_video or comparison != "B" or not bundle["guides"]):
        spatial_suffix = bind_spatial_prompt("", bundle, len(asset_files), spatial_input, sends_video,
                                            *([scene_flow] if scene_flow else []))
    extra_rules_length = len(CAMERA_GUIDE_RULES) + 2 if separated else 0
    shared_overhead = reference_prompt_overhead(
        prompt_assets, duration=options["duration"], reference_mode=prompt_mode,
        policy_text=generation_prompt + "\n" + scene_prompt,
    )
    binding_cost = len(spatial_suffix) + extra_rules_length + (2 if cast else 0) + (2 if camera_transcript else 0)
    if camera_transcript:
        camera_budget = capabilities.prompt_limit

        def save_camera_budget(record):
            metadata["promptTimeline"] = record
            update(metadata=metadata)

        compacted = await compact_timeline(
            metadata["previsTranscript"]["plan"], metadata["previsTranscript"]["sourceDuration"],
            options["duration"], request=optimize_request if api_key else None, model=review_model,
            max_chars=camera_budget, cached=metadata.get("promptTimeline"), save=save_camera_budget,
            check_owner=direction_owner_check, progress=lambda message: update(message=message),
            scene_flow_policy=scene_flow,
        )
        camera_transcript = compacted["text"]
        metadata["promptTimeline"] = compacted
    character_budget = capabilities.prompt_limit
    character_context = {**(guide_context or {}), "previsTimeline":
                         metadata.get("previsTranscript", {}).get("plan")}
    direction = await prepare_character_direction(
        optimize_request if api_key else None, store=self.store, cast=cast, model=review_model,
        user_prompt=options["prompt"], scene_prompt=scene_prompt, duration=options["duration"],
        source_identity=metadata["sourceIdentity"], context=character_context,
        cached=metadata.get("characterDirection"), save=save_direction,
        progress=lambda message: update(message=message), check_owner=direction_owner_check,
        max_chars=character_budget,
    )
    metadata["characterDirection"] = direction
    character_contract = direction_prompt(direction["cast"], direction["plan"]) if cast else ""
    scene_budget = capabilities.prompt_limit
    metadata["promptBudget"] = {
        "limit": capabilities.prompt_limit, "scope": "combined", "commonRules": shared_overhead,
        "camera": len(camera_transcript or ""), "characters": len(character_contract),
        "bindings": binding_cost, "sceneAndRules": scene_budget,
        "sceneContent": scene_budget - shared_overhead,
    }
    guide_context = {**(guide_context or {}), "characterDirection":
                     {key: direction[key] for key in ("status", "cast", "plan")}}
    update(metadata=metadata)
    if comparison:
        # Keep the scene and motion directions identical between A and B.
        common_prompt = (
            f'Scene and camera brief:\n{options.get("scenePrompt", "")}\n\n'
            f'Latest direction (takes priority):\n{options["prompt"]}\n\n'
            + (PREVIS_RULES + "\n" if sends_video else "")
            + "Preserve the intended cast and wardrobe throughout the shot. "
            "Use natural movement and follow the requested choreography and camera work."
        )
        if comparison == "A":
            reference_roles = []
            for index, asset in enumerate(asset_files, 1):
                label = asset.get("label") or asset.get("name") or asset["role"]
                reference_roles.append(f"Image {index}: {asset['role']} reference for {label}.")
            common_prompt += "\n\n" + "\n".join(reference_roles)
        metadata["identityComparison"] = {
            "requested": comparison,
            "comparable": comparison == "A" or bool(bundle["guides"]),
        }
        prepared = {"prompt": common_prompt, "optimization": "comparison-shared-prompt"}
    else:
        prepared = await optimize_video_prompt(
            generation_prompt,
            prompt_assets,
            scene_prompt=scene_prompt,
            duration=options["duration"],
            reference_mode="images" if spatial else mode,
            request=optimize_request if api_key else None,
            model=review_model,
            cached=(
                {**metadata, "submittedPrompt": metadata.get("optimizedBasePrompt", "")}
                if spatial
                else metadata
            ),
            guide_context=guide_context,
            max_length=scene_budget,
        )
    if spatial:
        # Optimizer cache stores the scene prompt; guide roles are bound once below.
        metadata["optimizedBasePrompt"] = prepared["prompt"]
        if sends_video or comparison != "B" or not metadata["identityComparison"]["comparable"]:
            prepared["prompt"] = bind_spatial_prompt(
                prepared["prompt"], bundle, len(asset_files), spatial_input, sends_video,
                *([scene_flow] if scene_flow else [])
            )
    reference_rules = ((camera_transcript,) if camera_transcript else
                       (PREVIS_RULES, CAMERA_GUIDE_RULES) if separated else
                       (PREVIS_RULES,) if sends_video else ())
    if presentation:
        reference_rules = (*reference_rules, presentation)
        prepared["prompt"] = attach_presentation_rules(prepared["prompt"], presentation)
    if scene_flow == FIRST_SCENE_CONTINUITY:
        reference_rules = (*reference_rules, SCENE_FLOW_RULES)
    if character_contract:
        reference_rules = (*reference_rules, character_contract)
        prepared["prompt"] = preserve_character_direction(prepared["prompt"], character_contract, None)
    if camera_transcript:
        prepared["prompt"] = preserve_timeline(prepared["prompt"], camera_transcript, None)
    if separated and CAMERA_GUIDE_RULES not in prepared["prompt"]:
        prepared["prompt"] += "\n\n" + CAMERA_GUIDE_RULES
    review_images = list(asset_files) if not spatial or spatial_input == "references" else []
    if spatial:
        review_images += [
            {
                "path": guide_directory / guide["name"],
                "role": "composition",
                "label": f'composition at {guide["time"]:.3f}s',
            }
            for guide in bundle["guides"]
        ]
    prepared["prompt"], input_report = await validate_video_request(
        optimize_request if api_key else None,
        model=review_model,
        prompt=prepared["prompt"],
        user_prompt=options["prompt"],
        scene_prompt=options.get("scenePrompt", ""),
        images=review_images,
        reference=model_reference if sends_video else None,
        reference_rules=reference_rules,
        directory=folder / "validation" / owner,
        prompt_limit=capabilities.prompt_limit,
        defer_length_check=True,
        guide_context=guide_context,
        character_direction={key: direction[key] for key in ("status", "cast", "plan")},
        cached=metadata.get("inputValidation"),
        progress=lambda message: update(message=message),
    )
    if camera_transcript:
        prepared["prompt"] = preserve_timeline(prepared["prompt"], camera_transcript, None)
    if character_contract:
        prepared["prompt"] = preserve_character_direction(prepared["prompt"], character_contract, None)
    prepared["prompt"] = attach_presentation_rules(prepared["prompt"], presentation)
    if scene_flow == FIRST_SCENE_CONTINUITY and SCENE_FLOW_RULES not in prepared["prompt"]:
        prepared["prompt"] += "\n\n" + SCENE_FLOW_RULES
    required_texts = [value for item in direction["cast"]
                      for value in (item["character_id"], item["voice_profile_id"])]
    required_texts += [entry["text"] for entry in (direction["plan"] or {}).get("dialogues", [])]
    if scene_flow == FIRST_SCENE_CONTINUITY:
        required_texts.append(SCENE_FLOW_RULES)
    if presentation:
        required_texts.append(presentation)
    if camera_transcript:
        required_texts += re.findall(r"\d+\.\d{3}–\d+\.\d{3}s", camera_transcript)
        required_texts += re.findall(r"Critical action \d+\.\d{3}s", camera_transcript)
    prepared["prompt"] = KOREAN_ATMOSPHERE_RULE + "\n\n" + prepared["prompt"]
    audio_mode = options.get("audioMode")
    if audio_mode:
        audio_rule = VOICE_ONLY if audio_mode == "bgm" else (
            "Audio: generate background music and the requested voices. Keep speech intelligible."
        )
        prepared["prompt"] = audio_rule + "\n\n" + prepared["prompt"]
        required_texts.append(audio_rule)

    def save_final_prompt(record):
        metadata["promptConsolidation"] = record
        update(metadata=metadata)

    metadata["promptBudget"]["beforeConsolidation"] = len(prepared["prompt"])
    fitted = await fit_video_prompt(
        prepared["prompt"], request=optimize_request if api_key else None, model=review_model,
        max_length=capabilities.prompt_limit, required_texts=required_texts,
        context={"characterDirection": {key: direction[key] for key in ("status", "cast", "plan")},
                 "generationRules": presentation},
        cached=metadata.get("promptConsolidation"), save=save_final_prompt,
        check_owner=direction_owner_check, progress=lambda message: update(message=message),
    )
    prepared["prompt"] = fitted["prompt"]
    metadata["promptConsolidation"] = fitted
    input_report["resolvedPrompt"] = prepared["prompt"]
    metadata["promptBudget"]["final"] = len(prepared["prompt"])
    logging.getLogger("uvicorn.error").info(
        "[video-prompt-budget] job=%s lengths=%s", job_id,
        json.dumps(metadata["promptBudget"], ensure_ascii=False),
    )
    metadata["inputValidation"] = input_report
    logging.getLogger("uvicorn.error").info(
        "[video-validation] job=%s input=%s reviews=%s issues=%s",
        job_id,
        input_report["status"],
        len(input_report["attempts"]),
        json.dumps(input_report["issues"], ensure_ascii=False),
    )
    metadata.update(
        submittedPrompt=prepared["prompt"],
        promptOptimization=prepared["optimization"],
        promptPolicyVersion=PROMPT_POLICY_VERSION,
    )
    update(metadata=metadata, message="참고 자료를 업로드하고 영상 생성을 준비합니다.")
    reference_url = await client.upload(model_reference, options["region"]) if sends_video else None
    ratio = "adaptive"
    if (
        (comparison or mode == "images" or spatial and spatial_input == "references")
        and metadata.get("width")
        and metadata.get("height")
    ):
        ratios = {
            "21:9": 21 / 9,
            "16:9": 16 / 9,
            "4:3": 4 / 3,
            "1:1": 1,
            "3:4": 3 / 4,
            "9:16": 9 / 16,
        }
        ratio = min(
            ratios, key=lambda key: abs(ratios[key] - metadata["width"] / metadata["height"])
        )
    reference_images = []
    for current in asset_files if not spatial or spatial_input == "references" else []:
        path = current["path"]
        url = await client.upload(
            path, options["region"], media_type=current.get("mediaType", "image/jpeg")
        )
        reference_images.append({"url": url, "role": current["role"]})
    guide_frames = []
    if spatial:
        for guide in bundle["guides"]:
            url = await client.upload(
                guide_directory / guide["name"], options["region"], media_type="image/png"
            )
            guide_frames.append({"url": url, "time": guide["time"]})
    reference_audio = []
    if options.get("audioMode") != "bgm":
        reference_audio = await upload_reference_audio(
            client, self.assets, options.get("audioAssets", []), options["region"]
        )
    update(
        metadata={
            **metadata,
            "submittedPrompt": prepared["prompt"],
            "referenceMode": mode,
            "referenceVideoSent": sends_video,
            "referenceVideoKind": "camera_staging" if separated else "original" if sends_video else None,
            "outputRatio": ratio,
            "spatialInput": spatial_input if spatial else None,
            "inputMediaTypes": (
                [
                    item["type"]
                    for item in client.build_media(
                        mode, spatial_input, reference_images, guide_frames, reference_url
                    )
                ]
                if mode in ("spatial", "spatial_motion")
                else ["reference_video"] * int(sends_video)
                + ["reference_image"] * len(reference_images)
            ),
            "promptPolicyVersion": PROMPT_POLICY_VERSION,
            "promptExtend": capabilities.prompt_extend,
        }
    )
    if not 1 <= len(prepared["prompt"]) <= capabilities.prompt_limit:
        raise PromptLengthError(
            f"통합 프롬프트는 선택 모델 기준 {capabilities.prompt_limit}자 이하여야 합니다."
        )
    await asyncio.to_thread(bind_source_identity, job["source"], reference, metadata["sourceIdentity"])
    # Commit this phase before the paid video request. A crash here must not resubmit.
    if not self.store.begin_submission(job_id, owner):
        raise LostLease()
    extra = (
        {"guide_frames": guide_frames, "spatial_input": spatial_input}
        if mode in ("spatial", "spatial_motion")
        else {}
    )
    if reference_audio:
        extra["reference_audio"] = reference_audio
    task_id = await client.submit(
        reference_url,
        generation_prompt,
        options["duration"],
        options["resolution"],
        options["region"],
        reference_images=reference_images,
        scene_prompt=scene_prompt,
        reference_mode=mode,
        ratio=ratio,
        final_prompt=prepared["prompt"],
        **extra,
    )
    update(provider_id=task_id, state="running", message="영상 생성 중입니다.")
    job = self.store.get(job_id)
    return job


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
    options = job["options"]
    job_id = job["id"]
    mode = options.get("referenceMode", "video")
    spatial_input = options.get("spatialInput", "references")
    requested_spatial_input = spatial_input
    api_key = self.service.config.get("OPENAI_API_KEY")
    capabilities = self.adapter_for(options).capabilities
    presentation = metadata.get("promptFilter", {}).get("generationRules", "")

    async def optimize_request(path, body):
        return await self._analysis_request(path, apply_presentation_rules(path, body, presentation))
    if not api_key:
        raise GuideError(
            "공간 가이드 생성에 OPENAI_API_KEY가 필요합니다. 기존 MP4 방식을 선택할 수 있습니다."
        )
    native = await self.native_scene_manifest(job["source"])

    def owner_check():
        if not self.store.heartbeat(job_id, owner=owner):
            raise LostLease()

    def progress(value):
        # Register derived guide files even when nobody opens their previews.
        for base in (
            guide_directory,
            guide_directory / "timeline",
            guide_directory / "candidates",
        ):
            for path in base.glob("*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and (
                        path.name == "manifest.json"
                        or re.fullmatch(
                            r"(?:layout|spatial)-[0-9]{1,2}\.png|(?:candidate|asset-part)-[0-9]{1,3}\.png",
                            path.name,
                        )
                    )
                ):
                    safe = self.service.archive.safe_path(path.relative_to(self.root))
                    key = f"guide-{job_id}-{base.name}-{path.name}"
                    self.service.archive.track(
                        key, safe, created=job["created"], reuse_path=True
                    )
        report = value.get("selection")
        if report and metadata.get("spatialSelectionLog") != report:
            logging.getLogger("uvicorn.error").info(
                "[spatial-scenes] job=%s selected=%s budget=%s scenes=%s",
                job_id,
                len(report["scenes"]),
                report["budget"],
                json.dumps(
                    [
                        {"time": scene["time"], "reasons": scene["reasons"]}
                        for scene in report["scenes"]
                    ],
                    ensure_ascii=False,
                ),
            )
            metadata["spatialSelectionLog"] = report
        metadata["spatialGuides"] = value
        if options.get("sceneFlowPolicy") == FIRST_SCENE_CONTINUITY and any(
            guide["index"] == 0 and guide["status"] == "excluded" for guide in value["guides"]
        ):
            update(metadata=metadata)
            raise GuideError("첫 씬 기준 이미지 검증 실패로 후속 이미지 생성을 중단했습니다.")
        expected = len(report["scenes"]) if report else options.get("spatialGuideCount", 3)
        verified = sum(item["status"] == "verified" for item in value["guides"])
        repairing = any(
            item["status"] == "invalid"
            or (item["status"] == "requesting" and item.get("generationAttempts", 0) > 1)
            for item in value["guides"]
        )
        if repairing:
            message = f"공간 가이드 검증 {verified}/{expected} · 구도 보정 중"
        else:
            message = f"공간 가이드 생성·검증 {verified}/{expected}"
        if options.get("previsTranscript"):
            message = message.replace("공간 가이드", "씬 참조 이미지"
                                      if options.get("sceneFlowPolicy") else "시작·종료 이미지")
        update(metadata=metadata, message=message)

    builder = SpatialGuideBuilder(
        optimize_request,
        OpenAIGuideImageEditor(
            api_key,
            model=self.service.config.get("SPATIAL_GUIDE_IMAGE_MODEL", "gpt-image-2"),
            transport=self.service.transport,
            presentation_rules=presentation,
        ),
        model=video_analysis_model(self.service, options),
        image_model=self.service.config.get("SPATIAL_GUIDE_IMAGE_MODEL", "gpt-image-2"),
    )
    scenario_guides = limit_scenario_frames(
        metadata.get("scenarioSnapshots", []), options.get("spatialGuideCount", 3),
    ) if spatial_input == "references" else []
    scenario_guides = [
        {key: scene[key] for key in ("index", "title", "fraction")} for scene in scenario_guides
    ]
    if options.get("sceneFlowPolicy") == FIRST_SCENE_CONTINUITY and scenario_guides:
        scenario_guides[0] = {**scenario_guides[0], "fraction": 0.0}
    bundle = await builder.build(
        reference,
        asset_files,
        guide_directory,
        count=options.get("spatialGuideCount", 3),
        input_format=spatial_input,
        brief=scene_prompt + "\n" + generation_prompt + (
            "\n" + timeline_prompt(metadata["previsTranscript"]["plan"],
                                    metadata["previsTranscript"]["sourceDuration"],
                                    options["duration"], scene_flow_policy=options.get("sceneFlowPolicy"))
            if options.get("previsTranscript") else ""
        ),
        scene_manifest=native,
        progress=progress,
        check_owner=owner_check,
        selection=options.get("spatialGuideSelection", "uniform"),
        **({"scenario_frames": scenario_guides} if scenario_guides else {}),
    )
    if options.get("sceneFlowPolicy") == FIRST_SCENE_CONTINUITY:
        try:
            accepted = continuous_guides(bundle["guides"])
        except ValueError as error:
            raise GuideError("첫 씬 기준 이미지를 검증하지 못했습니다. 후속 씬을 첫 장면으로 대체하지 않습니다.") from error
    elif options.get("previsTranscript"):
        try:
            accepted, warnings = endpoint_guides(bundle["guides"])
        except ValueError as error:
            raise GuideError("시작·종료 이미지 준비가 완료되지 않았습니다.") from error
        if not all((guide_directory / guide["name"]).is_file() for guide in accepted):
            raise GuideError("시작·종료 이미지 파일을 찾을 수 없습니다.")
        metadata["endpointQualityWarnings"] = warnings
        if warnings:
            logging.getLogger("uvicorn.error").info(
                "[endpoint-guides] job=%s 보정 필요 indices=%s · 생성된 두 프레임으로 계속 진행",
                job_id, warnings,
            )
        update(metadata=metadata)
    else:
        mode, spatial_input, accepted = verified_guide_inputs(
            mode, spatial_input, bundle["guides"], len(asset_files),
            capabilities.reference_image_limit,
        )
    if len(accepted) < len(bundle["guides"]):
        metadata["guideFallback"] = {
            "requestedInput": requested_spatial_input,
            "usedInput": spatial_input if accepted else mode,
            "usedGuideIndices": [guide["index"] for guide in accepted],
            "excludedGuideIndices": [
                guide["index"] for guide in bundle["guides"] if guide not in accepted
            ],
        }
        logging.getLogger("uvicorn.error").info(
            "[spatial-guides] job=%s continuing with %d verified guides using %s",
            job_id,
            len(accepted),
            metadata["guideFallback"]["usedInput"],
        )
    bundle = {**bundle, "guides": accepted}
    return bundle, mode, spatial_input


