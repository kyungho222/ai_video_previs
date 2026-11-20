"""Local media materialization, validation and upload endpoints."""

import asyncio
from io import BytesIO
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
from urllib.parse import urlparse
from uuid import uuid4

import httpcore
import httpx
from PIL import Image, UnidentifiedImageError
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from .video_errors import (
    ASSET_ID,
    AssetError,
    MAX_FORM_OVERHEAD,
    MAX_IMAGE_BYTES,
    MAX_REFERENCE_BYTES,
    ROLES,
    UUID,
    VideoError,
)

class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to the validated IP; HTTPcore retains the original TLS SNI name."""

    def __init__(self):
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            )
            if not addresses or any(
                not ipaddress.ip_address(row[4][0]).is_global for row in addresses
            ):
                raise ValueError()
        except (ValueError, OSError):
            raise VideoError("공개 IP가 아닌 영상 저장소 연결을 차단했습니다.") from None
        return await self.backend.connect_tcp(
            addresses[0][4][0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        raise VideoError("로컬 소켓 연결은 허용하지 않습니다.")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)



class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(retries=0)
        # HTTPX does not expose HTTPcore's backend in its public constructor.
        # Keep HTTPX's TLS/stream/error handling and replace only DNS+TCP creation.
        self._pool._network_backend = PublicNetworkBackend()



async def public_https(url):
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError()
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError()
    except (OSError, ValueError):
        raise VideoError("영상 주소는 외부에서 접근 가능한 HTTPS 주소여야 합니다.") from None
    return parsed



def inspect_video(path, *, max_duration=None, min_fps=0, check_dimensions=False):
    try:
        import cv2
    except ImportError:
        raise VideoError(
            "영상 검증에 필요한 기존 opencv-python-headless 의존성을 설치하세요."
        ) from None
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise VideoError("프리비즈 MP4를 읽을 수 없습니다.")
        fps = capture.get(cv2.CAP_PROP_FPS)
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not all(math.isfinite(x) and x > 0 for x in (fps, count, width, height)):
            raise VideoError("영상 길이·해상도 정보를 확인할 수 없습니다.")
        duration = count / fps
        if fps < min_fps or (
            check_dimensions
            and (
                not (240 <= width <= 4096 and 240 <= height <= 4096)
                or max(width / height, height / width) > 8
            )
        ):
            fps_requirement = f"{min_fps}fps 이상, " if min_fps else ""
            raise VideoError(
                f"참고 MP4는 {fps_requirement}가로·세로 240~4096px, 화면 비율 8:1 이내여야 합니다. (현재 {fps:.2f}fps, {int(width)}×{int(height)})"
            )
        if max_duration is not None and not 1 <= duration <= max_duration:
            raise VideoError(f"참고 영상 길이는 1~{max_duration}초여야 합니다.")
        return {"duration": duration, "fps": fps, "width": int(width), "height": int(height)}
    finally:
        capture.release()



class VideoAssets:
    def __init__(self, root, *, storage_subdir="runtime/data/wan-assets", max_images=10):
        self.root = Path(root) / storage_subdir
        self.max_images = max_images

    def save(self, content, role, name):
        if role not in ROLES:
            raise AssetError("에셋 유형은 캐릭터, 배경, 기타 중에서 선택하세요.")
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise AssetError("이미지 파일은 20MB 이하여야 합니다.")
        try:
            with Image.open(BytesIO(content)) as original:
                width, height = original.size
                if original.format not in ("JPEG", "PNG", "WEBP", "BMP"):
                    raise AssetError("JPG, PNG, WEBP, BMP 이미지를 사용하세요.")
                if (
                    not (240 <= width <= 8000 and 240 <= height <= 8000)
                    or max(width / height, height / width) > 8
                ):
                    raise AssetError(
                        "에셋 이미지는 가로·세로 240~8000px, 화면 비율 8:1 이내여야 합니다."
                    )
                image_format = original.format
                original.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise AssetError("유효한 이미지 파일을 읽을 수 없습니다.") from None
        asset_id = str(uuid4())
        extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp"}[image_format]
        media_type = Image.MIME[image_format]
        self.root.mkdir(parents=True, exist_ok=True)
        filename = str(name or "reference").replace("\\", "/").rsplit("/", 1)[-1][:120]
        metadata = {
            "id": asset_id,
            "role": role,
            "name": filename,
            "width": width,
            "height": height,
            "mediaType": media_type,
            "extension": extension,
            "originalBytes": True,
        }
        (self.root / (asset_id + extension)).write_bytes(content)
        (self.root / (asset_id + ".json")).write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        return metadata

    def get(self, asset_id):
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            raise AssetError("에셋 식별자가 올바르지 않습니다.")
        metadata = self.root / (asset_id + ".json")
        if not metadata.is_file() or metadata.is_symlink():
            raise FileNotFoundError("선택한 에셋 이미지를 찾을 수 없습니다.")
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AssetError("에셋 정보를 읽을 수 없습니다.") from None
        if data.get("id") != asset_id or data.get("role") not in ROLES:
            raise AssetError("에셋 정보가 올바르지 않습니다.")
        extension = data.get("extension", ".jpg")
        if extension not in (".jpg", ".png", ".webp", ".bmp"):
            raise AssetError("에셋 파일 형식이 올바르지 않습니다.")
        image = self.root / (asset_id + extension)
        if not image.is_file() or image.is_symlink():
            raise FileNotFoundError("선택한 에셋 이미지를 찾을 수 없습니다.")
        return data, image

    def resolve(self, asset_ids):
        if not isinstance(asset_ids, list) or len(asset_ids) > self.max_images:
            raise AssetError(f"참고 이미지는 총 {self.max_images}개까지 추가할 수 있습니다.")
        if any(
            not isinstance(value, str) or not ASSET_ID.fullmatch(value) for value in asset_ids
        ) or len(set(asset_ids)) != len(asset_ids):
            raise AssetError("에셋 식별자가 올바르지 않거나 중복되었습니다.")
        return [self.get(asset_id)[0] for asset_id in asset_ids]



class LimitedUploadRoute(APIRoute):
    """Bound the request before FastAPI spools multipart UploadFile objects."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def limited(request):
            is_image = request.url.path.endswith("/assets")
            is_video = request.url.path.endswith("/previs")
            if request.method != "POST" or not (is_image or is_video):
                return await handler(request)
            limit = (MAX_IMAGE_BYTES if is_image else MAX_REFERENCE_BYTES) + MAX_FORM_OVERHEAD
            message = (
                "에셋 업로드 요청이 20MB 제한을 초과합니다."
                if is_image
                else "프리비즈 업로드 요청이 100MB 제한을 초과합니다."
            )
            length = request.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > limit):
                raise HTTPException(413, detail=message)
            size, exceeded = 0, False

            async def receive():
                nonlocal size, exceeded
                message = await request.receive()
                size += len(message.get("body", b""))
                if size > limit:
                    exceeded = True
                    # Starlette closes in-progress spooled files for this error.
                    raise MultiPartException("프리비즈 업로드 요청 크기 제한을 초과했습니다.")
                return message

            try:
                return await handler(Request(request.scope, receive=receive))
            except StarletteHTTPException:
                if exceeded:
                    raise HTTPException(413, detail=message) from None
                raise

        return limited



class UploadedPrevis:
    def __init__(
        self, root, archive, *, storage_subdir="runtime/wan-inputs", api_prefix="/api/wan"
    ):
        self.directory = Path(root).resolve() / storage_subdir
        self.storage_subdir = storage_subdir
        self.api_prefix = api_prefix
        self.archive = archive

    def get(self, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(UUID, source_id):
            raise VideoError("업로드한 프리비즈 식별자가 올바르지 않습니다.")
        folder = self.directory / source_id
        record = folder / "source.json"
        if folder.is_symlink() or record.is_symlink():
            raise VideoError("업로드한 프리비즈 경로가 올바르지 않습니다.")
        path = self.archive.path("uploaded-" + source_id)
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FileNotFoundError("업로드한 프리비즈 정보를 찾을 수 없습니다.") from None
        if data.get("id") != source_id or path != folder / "previs.mp4":
            raise VideoError("업로드한 프리비즈 정보가 일치하지 않습니다.")
        asset = self.archive.record("uploaded-" + source_id)
        return {
            **data,
            "source": {"uploadedId": source_id},
            "videoUrl": self.api_prefix + "/previs/" + source_id + "/video",
            "expiresAt": asset["expiresAt"],
        }, path

    async def save(self, file, scene_prompt=""):
        name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1][:200]
        if not name.lower().endswith(".mp4"):
            raise VideoError("프리비즈는 MP4 파일로 등록하세요.")
        if len(scene_prompt) > 8000:
            raise VideoError("장면 설명은 8000자 이하여야 합니다.")
        source_id = str(uuid4())
        folder = self.directory / source_id
        # safe_path also rejects symlink ancestors before creating any files.
        target = self.archive.safe_path(Path(self.storage_subdir) / source_id / "previs.mp4")
        folder.mkdir(parents=True, exist_ok=False)
        partial = folder / "previs.part"
        record = folder / "source.json"
        published = False
        try:
            size = 0
            with partial.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    if size == 0 and chunk[4:8] != b"ftyp":
                        raise VideoError("올바른 MP4 파일이 아닙니다.")
                    size += len(chunk)
                    if size > MAX_REFERENCE_BYTES:
                        raise VideoError("프리비즈 MP4는 100MB 이하여야 합니다.", status_code=413)
                    output.write(chunk)
            if not size:
                raise VideoError("빈 MP4 파일은 등록할 수 없습니다.")
            try:
                metadata = await asyncio.to_thread(
                    inspect_video, partial, max_duration=None, min_fps=0, check_dimensions=False
                )
            except VideoError as error:
                metadata = {
                    "duration": None,
                    "fps": None,
                    "width": None,
                    "height": None,
                    "inspectionWarning": str(error),
                }
            data = {
                "id": source_id,
                "name": name,
                "metadata": metadata,
                "size": size,
                "scenePrompt": scene_prompt.strip(),
            }
            record.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            partial.replace(target)
            self.archive.track("uploaded-" + source_id, target)
            result, _ = self.get(source_id)
            published = True
            return result
        finally:
            partial.unlink(missing_ok=True)
            if not published:
                target.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                folder.rmdir()

    def recent(self):
        from .media_archive import MediaExpired

        records = sorted(
            self.directory.glob("*/source.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        result = []
        for record in records:
            if not re.fullmatch(UUID, record.parent.name):
                continue
            try:
                result.append(self.get(record.parent.name)[0])
            except (MediaExpired, FileNotFoundError, VideoError):
                continue
            if len(result) >= 30:
                break
        return result



def create_previs_upload_router(previs, *, api_prefix=None):
    from .media_archive import MediaExpired

    api_prefix = previs.api_prefix if api_prefix is None else api_prefix
    router = APIRouter(route_class=LimitedUploadRoute)

    @router.post(api_prefix + "/previs", status_code=201)
    async def upload_previs(
        file: UploadFile = File(...), scene_prompt: str = Form("", alias="scenePrompt")
    ):
        try:
            return await previs.save(file, scene_prompt)
        except VideoError as error:
            raise HTTPException(error.status_code or 400, detail=str(error)) from None
        finally:
            await file.close()

    @router.get(api_prefix + "/previs")
    async def list_previs():
        return {"sources": await asyncio.to_thread(previs.recent)}

    @router.get(api_prefix + "/previs/{source_id}/video")
    async def preview_previs(source_id: str):
        try:
            _, path = previs.get(source_id)
        except MediaExpired as error:
            raise HTTPException(410, detail=str(error)) from None
        except FileNotFoundError:
            raise HTTPException(404, detail="등록된 프리비즈 파일이 없습니다.") from None
        except VideoError as error:
            raise HTTPException(400, detail=str(error)) from None
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
        )

    return router



async def download_video(
    url, target, *, headers=None, max_bytes=MAX_REFERENCE_BYTES, transport=None
):
    await public_https(url)
    partial = Path(str(target) + ".part")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90, connect=15),
            follow_redirects=False,
            trust_env=False,
            transport=transport or PublicHTTPTransport(),
        ) as http:
            async with http.stream("GET", url, headers=headers or {}) as response:
                if not response.is_success:
                    raise VideoError(
                        f"영상 다운로드 실패 또는 링크 만료 (HTTP {response.status_code}).",
                        status_code=response.status_code,
                    )
                length = response.headers.get("content-length")
                if length and max_bytes is not None and int(length) > max_bytes:
                    raise VideoError("영상 파일 크기 제한을 초과했습니다.")
                size = 0
                with partial.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise VideoError("영상 파일 크기 제한을 초과했습니다.")
                        output.write(chunk)
        with partial.open("rb") as content:
            if b"ftyp" not in content.read(32):
                raise VideoError("다운로드한 결과가 MP4 파일이 아닙니다.")
        partial.replace(target)
    except httpx.RequestError:
        raise VideoError("영상 파일 다운로드 연결에 실패했습니다.") from None
    finally:
        partial.unlink(missing_ok=True)

