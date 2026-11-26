"""Alibaba Wan video adapter: credentials, uploads, provider payloads and polling."""

import re
from urllib.parse import quote, urlparse
import logging
import httpx
from .video_errors import (
    AUDIO_MEDIA_TYPES, MAX_AUDIO_BYTES, MAX_AUDIO_CLIPS, MAX_IMAGE_BYTES,
    MAX_REFERENCE_BYTES, PromptLengthError, VideoError as WanError,
)
from .video_media import PublicHTTPTransport, public_https, download_video
from .video_prompt import build_reference_prompt
from .video_models import VideoCapabilities
from .ai_usage import TERMINAL_VIDEO_STATES

PROMPT_EXTEND = False
MODEL = "wan3.0-video"
REGIONS = {
    "singapore": "https://dashscope-intl.aliyuncs.com",
    "beijing": "https://dashscope.aliyuncs.com",
}


def provider_result_url(url):
    """Use TLS for provider OSS links, preserving the signed path/query bytes."""
    try:
        parsed=urlparse(url)
        if (parsed.scheme=='http' and parsed.port is None and not parsed.username and not parsed.password
                and re.fullmatch(r'[a-z0-9][a-z0-9-]*\.oss-[a-z0-9-]+\.aliyuncs\.com',parsed.hostname or '')):
            logging.getLogger('uvicorn.error').info('[video-download] 제공사 결과 주소 HTTPS 정규화 · host=%s',parsed.hostname)
            return 'https'+url[len(parsed.scheme):]
    except (ValueError,TypeError,AttributeError):
        pass
    return url


def wan_guide_media(mode, input_format, assets, guides, motion_url):
    if mode not in ("spatial", "spatial_motion") or input_format not in (
        "references",
        "first_frame",
        "first_last",
    ):
        raise WanError("공간 가이드 입력 방식을 확인하세요.")
    if not guides:
        raise WanError("에셋이 적용된 공간 가이드 이미지가 없습니다.")
    if mode == "spatial_motion" and (input_format != "references" or not motion_url):
        raise WanError("동작 영상은 공간 참조 이미지 방식에서만 함께 전달할 수 있습니다.")
    if input_format == "first_frame":
        return [{"type": "first_frame", "url": guides[0]["url"]}]
    if input_format == "first_last":
        if len(guides) != 2:
            raise WanError("시작·종료 이미지 방식에는 두 가이드가 필요합니다.")
        return [
            {"type": "first_frame", "url": guides[0]["url"]},
            {"type": "last_frame", "url": guides[-1]["url"]},
        ]
    if len(assets) + len(guides) > 10:
        raise WanError(
            "공급사 참조 이미지 한도는 10장입니다. 에셋과 공간 가이드의 합계를 줄이거나 시작 이미지 방식을 선택하세요."
        )
    media = [{"type": "reference_image", "url": item["url"]} for item in [*assets, *guides]]
    if mode == "spatial_motion":
        media.append({"type": "reference_video", "url": motion_url})
    return media


def build_wan_prompt(
    user_prompt, reference_images=None, *, scene_prompt="", duration=None, reference_mode="video"
):
    return build_reference_prompt(
        user_prompt,
        reference_images,
        scene_prompt=scene_prompt,
        duration=duration,
        reference_mode=reference_mode,
        max_length=20000,
    )


class WanClient:
    capabilities = VideoCapabilities(
        provider="wan",
        label="Wan 3.0",
        model=MODEL,
        regions=tuple(REGIONS),
        reference_audio_limit=MAX_AUDIO_CLIPS,
    )
    build_media = staticmethod(wan_guide_media)

    @staticmethod
    def failure_message(result):
        return provider_failure_message(result)

    def __init__(self, api_key, *, transport=None):
        self.api_key = api_key
        self.transport = transport

    @property
    def configured(self):
        return bool(self.api_key)

    def base(self, region):
        if region not in REGIONS:
            raise WanError("API 키의 리전은 Singapore 또는 Beijing을 선택하세요.")
        if not self.api_key:
            raise WanError(".env에 DASHSCOPE_API_KEY 또는 ALIBABA_API_KEY를 설정하세요.")
        return REGIONS[region]

    def http(self, *, public=False):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(90, connect=15),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport or (PublicHTTPTransport() if public else None),
        )

    def check(self, response, action):
        if response.status_code in (401, 403):
            raise WanError(
                "Wan API 인증·권한 오류입니다. API 키의 리전과 Wan 3.0 사용 권한을 확인하세요."
            )
        if response.status_code == 429:
            raise WanError("Wan 요청 한도에 도달했습니다. 잠시 후 작업 상태를 확인하세요.")
        if not response.is_success:
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            safe_code = (
                ", " + code
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code)
                else ""
            )
            raise WanError(f"Wan {action} 실패 (HTTP {response.status_code}{safe_code}).")
        try:
            return response.json()
        except ValueError:
            raise WanError(f"Wan {action} 응답 형식이 올바르지 않습니다.") from None

    async def upload(self, path, region, *, media_type="video/mp4"):
        base = self.base(region)
        if media_type == "video/mp4":
            limit = MAX_REFERENCE_BYTES
        elif media_type in AUDIO_MEDIA_TYPES.values():
            limit = MAX_AUDIO_BYTES
        elif media_type.startswith("image/"):
            limit = MAX_IMAGE_BYTES
        else:
            limit = 0
        if not 0 < path.stat().st_size <= limit:
            raise WanError("참고 MP4는 100MB, 이미지는 20MB, 음원은 10MB 이하여야 합니다.")
        try:
            async with self.http() as http:
                response = await http.get(
                    base + "/api/v1/uploads",
                    params={"action": "getPolicy", "model": MODEL},
                    headers={"Authorization": "Bearer " + self.api_key},
                )
                policy = self.check(response, "업로드 권한 조회").get("data", {})
                host = policy.get("upload_host", "")
                parsed = await public_https(host)
                if not parsed.hostname.endswith(".aliyuncs.com"):
                    raise WanError("공식 Alibaba 업로드 저장소 주소가 아닙니다.")
                required = (
                    "upload_dir",
                    "oss_access_key_id",
                    "signature",
                    "policy",
                    "x_oss_object_acl",
                    "x_oss_forbid_overwrite",
                )
                if not all(isinstance(policy.get(k), str) and policy[k] for k in required):
                    raise WanError("Wan 임시 업로드 권한 정보가 비어 있습니다.")
                key = policy["upload_dir"].rstrip("/") + "/" + path.name
                form = {
                    "OSSAccessKeyId": policy["oss_access_key_id"],
                    "Signature": policy["signature"],
                    "policy": policy["policy"],
                    "x-oss-object-acl": policy["x_oss_object_acl"],
                    "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
                    "key": key,
                    "success_action_status": "200",
                }
                with path.open("rb") as source:
                    async with self.http(public=True) as storage:
                        response = await storage.post(
                            host, data=form, files={"file": (path.name, source, media_type)}
                        )
                if not response.is_success:
                    raise WanError(f"참고 자료 업로드 실패 (HTTP {response.status_code}).")
                return "oss://" + key
        except httpx.RequestError:
            raise WanError(
                "Wan 참고 자료 업로드 연결에 실패했습니다. 리전과 네트워크를 확인하세요."
            ) from None

    async def submit(
        self,
        reference,
        prompt,
        duration,
        resolution,
        region,
        *,
        reference_images=None,
        reference_audio=None,
        scene_prompt="",
        reference_mode="video",
        ratio="adaptive",
        final_prompt=None,
        guide_frames=None,
        spatial_input="references",
    ):
        base = self.base(region)
        if reference_mode not in ("video", "images", "spatial", "spatial_motion") or (
            reference_mode == "video" and not reference
        ):
            raise WanError("참고 입력 방식을 확인하세요.")
        if reference_mode == "images" and not reference_images:
            raise WanError("이미지 전용 생성에는 참고 에셋이 필요합니다.")
        if reference_audio and (
            len(reference_audio) > self.capabilities.reference_audio_limit
            or (reference_mode in ("spatial", "spatial_motion") and spatial_input != "references")
        ):
            raise WanError("참고 음원은 참조 모드에서 최대 5개까지 전달할 수 있습니다.")
        if reference_mode in ("spatial", "spatial_motion"):
            media = wan_guide_media(
                reference_mode, spatial_input, reference_images or [], guide_frames or [], reference
            )
        else:
            media = (
                [{"type": "reference_video", "url": reference}] if reference_mode == "video" else []
            )
            if reference_images:
                for image in reference_images:
                    media.append({"type": "reference_image", "url": image["url"]})
        media.extend({"type": "reference_audio", "url": url} for url in reference_audio or ())
        if final_prompt is None:
            prompt = build_wan_prompt(
                prompt,
                reference_images,
                scene_prompt=scene_prompt,
                duration=duration,
                reference_mode=reference_mode,
            )
        else:
            if (
                not isinstance(final_prompt, str)
                or not final_prompt.strip()
                or len(final_prompt) > 20000
            ):
                raise PromptLengthError("최종 영상 프롬프트의 내용과 20000자 제한을 확인하세요.")
            prompt = final_prompt
        body = {
            "model": MODEL,
            "input": {"prompt": prompt, "media": media},
            "parameters": {
                "duration": duration,
                "resolution": resolution,
                "ratio": ratio,
                "audio": True,
                "prompt_extend": PROMPT_EXTEND,
            },
        }
        try:
            async with self.http() as http:
                response = await http.post(
                    base + "/api/v1/services/aigc/video-generation/video-synthesis",
                    headers={
                        "Authorization": "Bearer " + self.api_key,
                        "X-DashScope-Async": "enable",
                        "X-DashScope-OssResourceResolve": "enable",
                    },
                    json=body,
                )
                data = self.check(response, "생성 요청")
        except httpx.RequestError:
            raise WanError(
                "Wan 생성 요청의 접수 여부를 확인할 수 없습니다. 자동 재요청하지 않았습니다. 공급사 작업 내역을 먼저 확인하세요."
            ) from None
        task_id = data.get("output", {}).get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise WanError(
                "Wan 작업 ID를 확인할 수 없습니다. 자동 재요청하지 않았습니다. 공급사 작업 내역을 확인하세요."
            )
        return task_id

    async def poll(self, task_id, region):
        base = self.base(region)
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise WanError("Wan 작업 ID가 올바르지 않습니다.")
        try:
            async with self.http() as http:
                response = await http.get(
                    base + "/api/v1/tasks/" + quote(task_id),
                    headers={"Authorization": "Bearer " + self.api_key},
                )
                data = self.check(response, "상태 조회")
                result = data.get("output", {})
                if isinstance(result, dict) and result.get("task_status") in TERMINAL_VIDEO_STATES:
                    return {**result, "_usage": data.get("usage"), "_request_id": data.get("request_id")}
                return result
        except httpx.RequestError:
            raise WanError(
                "Wan 작업 상태를 조회할 수 없습니다. 작업을 다시 생성하지 말고 상태 조회를 다시 시도하세요."
            ) from None

    async def download(self, url, target, *, headers=None, max_bytes=MAX_REFERENCE_BYTES):
        return await download_video(
            provider_result_url(url), target, headers=headers, max_bytes=max_bytes, transport=self.transport
        )


def provider_failure_message(result):
    code = result.get("code")
    safe_code = (
        code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else "FAILED"
    )
    detail = result.get("message")
    duration = (
        re.search(
            r"duration should be at most (\d{1,8}(?:\.\d{1,6})?)s,\s*got (\d{1,8}(?:\.\d{1,6})?)s",
            detail[:10000],
            re.I,
        )
        if isinstance(detail, str)
        else None
    )
    if duration:
        return f"Wan 공급사가 참고 영상 길이를 거절했습니다: 최대 {duration[1]}초, 전달된 원본 {duration[2]}초 ({safe_code})."
    return "Wan 작업이 완료되지 않았습니다 (" + safe_code + "). 공급사 작업 내역을 확인하세요."


def create_wan_router(service):
    from .video_generation import create_video_router

    return create_video_router(service)


def __getattr__(name):
    if name == "WanManager":
        from . import video_generation

        return video_generation.VideoManager
    raise AttributeError(name)


def verified_guide_inputs(mode, spatial_input, guides, asset_count):
    from .video_generation import verified_guide_inputs as select_inputs

    return select_inputs(
        mode, spatial_input, guides, asset_count, WanClient.capabilities.reference_image_limit
    )
