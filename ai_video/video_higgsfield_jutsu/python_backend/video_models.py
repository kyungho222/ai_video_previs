"""Video model capabilities and adapter registration."""

from dataclasses import dataclass
from typing import Callable, Protocol
from .video_errors import VideoError


@dataclass(frozen=True)
class VideoCapabilities:
    provider: str
    label: str
    model: str
    regions: tuple[str, ...]
    resolutions: tuple[str, ...] = ("480P", "720P", "1080P")
    reference_modes: tuple[str, ...] = ("images", "video", "spatial", "spatial_motion")
    spatial_inputs: tuple[str, ...] = ("references", "first_frame", "first_last")
    reference_image_limit: int = 10
    prompt_limit: int = 20000
    image_min_side: int = 240
    image_max_side: int = 8000
    image_max_aspect: float = 8
    prompt_extend: bool = False
    reference_audio_limit: int = 0

    @property
    def image_constraints(self):
        return {
            "min_side": self.image_min_side,
            "max_side": self.image_max_side,
            "max_aspect": self.image_max_aspect,
        }


class VideoAdapter(Protocol):
    """Adapters return a task ID from submit and normalized status from poll.

    poll returns task_status (SUCCEEDED/FAILED/CANCELED/UNKNOWN or a pending state),
    plus video_url on success. Upload and download hide provider storage details.
    """

    capabilities: VideoCapabilities
    configured: bool

    async def upload(self, path, region, *, media_type="video/mp4"): ...
    async def submit(self, reference, prompt, duration, resolution, region, **kwargs): ...
    async def poll(self, task_id, region): ...
    async def download(self, url, target, **kwargs): ...
    def build_media(self, mode, spatial_input, assets, guides, motion_url): ...
    def failure_message(self, result): ...


_FACTORIES: dict[str, Callable] = {}


def register_video_adapter(provider, factory):
    if not provider or not callable(factory):
        raise ValueError("A provider identifier and adapter factory are required")
    _FACTORIES[provider] = factory


def register_builtin_adapters():
    if "wan" not in _FACTORIES:
        from .wan import WanClient

        register_video_adapter(
            "wan",
            lambda config, transport: WanClient(
                config.get("DASHSCOPE_API_KEY") or config.get("ALIBABA_API_KEY"),
                transport=transport,
            ),
        )


def video_adapters(config, transport=None):
    register_builtin_adapters()
    return {name: factory(config, transport) for name, factory in _FACTORIES.items()}


def validate_video_input(adapter, mode, spatial_input, duration, resolution, region, image_count):
    capabilities = adapter.capabilities
    if mode not in capabilities.reference_modes:
        raise VideoError("선택한 영상모델이 해당 참조 입력 방식을 지원하지 않습니다.")
    if mode in ("spatial", "spatial_motion") and spatial_input not in capabilities.spatial_inputs:
        raise VideoError("선택한 영상모델이 해당 공간 이미지 입력을 지원하지 않습니다.")
    if resolution not in capabilities.resolutions or region not in capabilities.regions:
        raise VideoError("선택한 영상모델의 해상도·리전 설정을 확인하세요.")
    if type(duration) is not int or duration <= 0:
        raise VideoError("영상 길이는 양의 정수여야 합니다.")
    if image_count > capabilities.reference_image_limit:
        raise VideoError(
            f"선택한 영상모델의 참조 이미지 한도는 {capabilities.reference_image_limit}장입니다."
        )
